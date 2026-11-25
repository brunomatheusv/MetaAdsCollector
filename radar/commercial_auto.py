"""Bounded verification of single, one-time-priced digital offers."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from .lp_evidence import _clean, _Signals, _web_link, extract_evidence
from .lp_http import LPFetchError, fetch_page
from .normalize import canonical_url


class _CommercialSignals(_Signals):
    def __init__(self):
        super().__init__()
        self.documents = []

    def _read_products(self, tree):
        self.documents.append(tree)


def _nodes(documents):
    stack = list(documents)
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
        if isinstance(node, dict):
            yield node
            stack.extend(v for v in node.values() if isinstance(v, (list, dict)))


def _types(node):
    values = node.get("@type", [])
    return {
        item.rsplit("/", 1)[-1] for item in (values if isinstance(values, list) else [values]) if isinstance(item, str)
    }


def _product(parser):
    products = [
        node
        for node in _nodes(parser.documents)
        if _types(node) & {"Course", "Book", "DigitalDocument", "Product", "SoftwareApplication"}
    ]
    unique = []
    for product in products:
        if product not in unique:
            unique.append(product)
    if len(unique) == 1 and not parser.ambiguity:
        return unique[0]
    return {}


def _field(value):
    if not isinstance(value, str):
        return None
    return _clean(re.sub(r"<[^>]*>", " ", value)) or None


def _price(offer):
    if not isinstance(offer, dict) or _types(offer) != {"Offer"}:
        return None
    if any(key in offer for key in ("priceSpecification", "billingDuration", "billingIncrement", "eligibleDuration")):
        return None
    if offer.get("availability") not in ("InStock", "https://schema.org/InStock", "http://schema.org/InStock"):
        return None
    try:
        amount = Decimal(str(offer.get("price")))
    except InvalidOperation:
        return None
    currency = offer.get("priceCurrency")
    if currency == "BRL" and amount.is_finite() and 0 < amount <= Decimal("1000000"):
        return amount
    return None


def _format(product):
    types = _types(product)
    if "Course" in types and product.get("courseMode") in ("online", "Online"):
        return "Curso online"
    if "Book" in types and product.get("bookFormat") in ("EBook", "https://schema.org/EBook"):
        return "E-book"
    if "DigitalDocument" in types:
        return "Material digital"
    return None


def _seller_br(product, offer):
    seller = offer.get("seller", product.get("seller", {}))
    addr = seller.get("address", {}) if isinstance(seller, dict) else {}
    return isinstance(addr, dict) and addr.get("addressCountry") in ("BR", "BRA", "Brasil", "Brazil")


def _parser(response):
    parser = _CommercialSignals()
    parser.feed(response.body.decode("utf-8", errors="replace"))
    parser.close()
    return parser


class CommercialInspection:
    """Serial, bounded public GETs. HTTP denies pause the host for this run."""

    def __init__(self, ceiling, *, fetch=fetch_page, sleep=time.sleep):
        self.ceiling = Decimal(str(ceiling)) if ceiling is not None else None
        self.fetch = fetch
        self.sleep = sleep
        self.pages = {}
        self.attempts = 0
        self.requests = 0
        self.hosts = set()
        self.blocked = set()
        self.errors = {}

    def _pace(self, host):
        if host in self.blocked or self.requests >= 36:
            raise LPFetchError("commercial_budget_or_host_paused")
        if self.requests:
            self.sleep(1)
        self.requests += 1
        self.hosts.add(host)

    def _page(self, url):
        if url not in self.pages:
            if self.attempts >= 24:
                return None
            self.attempts += 1
            self.pages[url] = None
            try:
                self.pages[url] = self.fetch(url, timeout=8, max_redirects=2, before_request=self._pace)
            except LPFetchError as exc:
                kind = str(exc)
                self.errors[kind] = self.errors.get(kind, 0) + 1
                if exc.status in {401, 403, 429}:
                    self.blocked.add(urlsplit(exc.chain[-1] if exc.chain else url).hostname)
        return self.pages[url]

    def inspect(self, row) -> dict:
        if len(row["destinations"]) != 1:
            return {"reason": "Destino ausente ou ambíguo."}
        start = row["destinations"][0]
        response = self._page(start)
        if not response:
            return {"reason": "Página indisponível ou orçamento de inspeção atingido."}
        try:
            if canonical_url(start)[2] != canonical_url(response.url)[2] or set(
                extract_evidence(response, start)["conflicts"]
            ) - {"structured_url_conflicts_with_destination"}:
                return {"reason": "Destino ou sinais de identidade conflitantes."}
            product = _product(_parser(response))
            name = _field(product.get("name"))
            if "SoftwareApplication" in _types(product):
                return {
                    "category": "SOFTWARE_SAAS",
                    "name": name,
                    "lp": response.url,
                    "sources": [response.url],
                    "reason": "Página de software.",
                }
            declared = product.get("url")
            if (
                isinstance(declared, str)
                and canonical_url(_web_link(response.url, declared))[2] != (canonical_url(response.url)[2])
            ):
                return {"reason": "URL declarada para o produto diverge da página observada."}
            offer = product.get("offers")
            form = _format(product)
            if not form or not name or not isinstance(offer, dict):
                return {"reason": "Formato digital e oferta única não verificáveis na página."}
            amount = _price(offer)
            checkout = _web_link(response.url, offer["url"]) if isinstance(offer.get("url"), str) else None
            if amount is None or not _seller_br(product, offer) or not checkout or checkout == response.url:
                return {"reason": "Preço único em BRL, vendedor brasileiro ou checkout não comprovado."}
            payment = self._page(checkout)
            if not payment:
                return {"reason": "Checkout indisponível ou orçamento de inspeção atingido."}
            extract_evidence(payment, checkout)
            paid = _product(_parser(payment))
            sku = _field(product.get("sku"))
            if (
                not sku
                or sku != _field(paid.get("sku"))
                or name != _field(paid.get("name"))
                or amount != _price(paid.get("offers"))
            ):
                return {"reason": "Identidade e valor não coincidem inequivocamente no checkout."}
        except (LPFetchError, ValueError, RecursionError):
            return {"reason": "HTML inválido ou evidência conflitante."}
        return {
            "category": "INFOPRODUTO_LOW_TICKET_BR"
            if self.ceiling is not None and amount <= self.ceiling
            else "INDETERMINADO",
            "name": name,
            "niche": _field(product.get("category")),
            "audience": _field(product.get("audience", {}).get("audienceType"))
            if isinstance(product.get("audience"), dict)
            else None,
            "format": form,
            "promise": _field(product.get("description")),
            "price": float(amount),
            "currency": "BRL",
            "price_verified": True,
            "market": "BR",
            "lp": response.url,
            "checkout": payment.url,
            "sources": [response.url, payment.url],
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "reason": f"Formato digital, vendedor brasileiro e preço no checkout comprovados pelo SKU {sku}."
            if self.ceiling is not None and amount <= self.ceiling
            else "Preço confirmado; teto ausente ou excedido.",
            "evidence_source": "structured_lp_and_checkout",
        }
