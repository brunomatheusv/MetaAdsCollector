"""Deterministic landing-page signals, without executing or retaining HTML."""

from __future__ import annotations

import hashlib
import json
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urljoin, urlsplit

from .lp_http import LPFetchError, PageResponse, public_url
from .normalize import canonical_url, destination_kind

IDENTITY_VERSION = 1
NON_OFFER_PATHS = {
    "about",
    "sobre",
    "privacy",
    "privacy-policy",
    "terms",
    "terms-of-service",
    "login",
    "signup",
    "contact",
    "contato",
    "help",
    "support",
    "404",
    "not-found",
}
PRODUCT_TYPES = {"Product", "Offer", "IndividualProduct", "ProductGroup", "AggregateOffer"}
IDENTIFIERS = ("productID", "sku", "mpn", "gtin", "gtin13", "identifier", "@id")
GENERIC_TEXT = {
    "home",
    "inicio",
    "página inicial",
    "landing page",
    "index",
    "site",
    "welcome",
    "bem vindo",
    "checkout",
}
BLOCKED_TITLES = {
    "just a moment",
    "access denied",
    "attention required",
    "verify you are human",
    "forbidden",
    "page not found",
    "404 not found",
    "página não encontrada",
    "request blocked",
}


def _clean(text: str) -> str:
    return " ".join(text.split())[:240]


class _Signals(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.copy: dict[str, list[str]] = {}
        self.links: dict[str, list[str]] = {}
        self.products: list[dict[str, str]] = []
        self.capture: str | None = None
        self.parts: list[str] = []
        self.ambiguity = False

    @staticmethod
    def _add(bucket: dict[str, list[str]], key: str, value: str) -> None:
        entries = bucket.setdefault(key, [])
        if value and value not in entries and len(entries) < 8:
            entries.append(value)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = dict(attrs)
        if tag in {"title", "h1", "h2"}:
            self.capture, self.parts = tag, []
        elif tag == "script" and (data.get("type") or "").lower() == "application/ld+json":
            self.capture, self.parts = "script", []
        elif tag == "meta":
            prop = (data.get("property") or "").lower()
            if prop in {"og:title", "og:site_name"}:
                self._add(self.copy, prop, _clean(data.get("content") or ""))
            if prop == "og:url":
                self._add(self.links, prop, (data.get("content") or "")[:4096])
        elif tag == "link" and "canonical" in (data.get("rel") or "").lower().split():
            self._add(self.links, "canonical", (data.get("href") or "")[:4096])
        elif tag == "form":
            self._add(self.links, "form_actions", (data.get("action") or "")[:4096])
        elif tag == "a" and (href := data.get("href")) and re.search(r"checkout|buy|pay\.", href, re.I):
            self._add(self.links, "checkout_links", href[:4096])

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != self.capture:
            return
        text = "".join(self.parts)
        self.capture, self.parts = None, []
        if tag != "script":
            self._add(self.copy, tag, _clean(text))
            return
        try:
            self._read_products(json.loads(text))
        except (ValueError, RecursionError):
            self.ambiguity = True

    def _read_products(self, tree: object) -> None:
        stack = [tree]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            if not isinstance(node, dict):
                continue
            raw_types = node.get("@type", [])
            types = raw_types if isinstance(raw_types, list) else [raw_types]
            matched = {item.rsplit("/", 1)[-1] for item in types if isinstance(item, str)} & PRODUCT_TYPES
            if matched:
                fields = {"type": sorted(matched)[0]}
                for field in ("name", "url", *IDENTIFIERS):
                    value = node.get(field)
                    if isinstance(value, dict) and field == "identifier":
                        value = value.get("value")
                    if isinstance(value, (str, int)) and str(value).strip():
                        fields[field] = _clean(str(value))
                if fields not in self.products:
                    self.products.append(fields)
                    if len(self.products) > 12:
                        self.ambiguity = True
                        return
            stack.extend(value for value in node.values() if isinstance(value, (dict, list)))


def _web_link(base: str, value: str) -> str | None:
    try:
        return public_url(urljoin(base, value))[0]
    except (ValueError, LPFetchError):
        return None


def _compatible_query(left: str, right: str) -> bool:
    left_query = parse_qsl(urlsplit(left).query, keep_blank_values=True)
    right_query = parse_qsl(urlsplit(right).query, keep_blank_values=True)
    return not left_query or left_query == right_query


def _specific_offer_destination(host: str | None, key: str | None) -> bool:
    if not host or not key or destination_kind(host, key) != "SPECIFIC_PATH":
        return False
    tail = urlsplit(f"https://{key}").path.rstrip("/").rsplit("/", 1)[-1].lower()
    return tail.removesuffix(".html") not in NON_OFFER_PATHS


def extract_evidence(response: PageResponse, requested_url: str) -> dict:
    if not re.search(rb"<(?:!doctype|html|head|body|title|h[12]|meta|link|script)(?=[\s>])", response.body, re.I):
        raise LPFetchError("invalid_html")
    parser = _Signals()
    try:
        parser.feed(response.body.decode("utf-8", errors="replace"))
        parser.close()
    except (ValueError, AssertionError, RecursionError) as exc:
        raise LPFetchError("invalid_html") from exc
    copy = parser.copy
    signal_text = [value for key, values in copy.items() if key != "og:site_name" for value in values]
    if any(re.sub(r"[^\w ]+", "", phrase.lower()) in BLOCKED_TITLES for phrase in signal_text):
        raise LPFetchError("challenge_or_error_page")
    if b"cf-chl-" in response.body.lower() or b"/cdn-cgi/challenge-platform/" in response.body.lower():
        raise LPFetchError("challenge_or_error_page")

    links = {
        kind: sorted({url for item in items if (url := _web_link(response.url, item))})
        for kind, items in parser.links.items()
    }
    final, host, final_key = canonical_url(response.url)
    requested, _, _ = canonical_url(requested_url)
    canonical_candidates = {canonical_url(url) for url in links.get("canonical", [])}
    conflicts = []
    canonicals = {(domain, key): url for url, domain, key in sorted(canonical_candidates)}
    canonical = next(iter(canonicals.values())) if len(canonicals) == 1 else None
    if len(canonicals) > 1:
        conflicts.append("multiple_canonical_urls")

    final_specific = _specific_offer_destination(host, final_key)
    anchor = final_key if final_specific else None
    if canonical:
        _, canonical_host, canonical_key = canonical_url(canonical)
        if canonical_host != host or not _compatible_query(final, canonical):
            conflicts.append("canonical_changes_host_or_functional_parameters")
        elif _specific_offer_destination(canonical_host, canonical_key):
            if final_specific and canonical_key != final_key:
                conflicts.append("canonical_conflicts_with_specific_final_url")
            else:
                anchor = canonical_key
    if not _compatible_query(requested, final):
        conflicts.append("redirect_changes_functional_parameters")

    products = [p for p in parser.products if p["type"] in {"Product", "IndividualProduct", "ProductGroup"}]
    offers = [p for p in parser.products if p["type"] in {"Offer", "AggregateOffer"}]
    if (
        len(products) > 1
        or len(offers) > 1
        or any(p["type"] in {"ProductGroup", "AggregateOffer"} for p in parser.products)
        or parser.ambiguity
    ):
        conflicts.append("ambiguous_structured_offers")
    item = (products or offers or [{}])[0]
    product_id = {field: item[field] for field in IDENTIFIERS if field in item and field != "@id"}
    if item_url := item.get("url"):
        structured_link = _web_link(response.url, item_url)
        if structured_link:
            _, structured_host, structured_key = canonical_url(structured_link)
            if structured_host != host or (anchor and structured_key != anchor):
                conflicts.append("structured_url_conflicts_with_destination")
    meaningful_text = any(len(value) >= 5 and value.lower() not in GENERIC_TEXT for value in signal_text)
    reasons = []
    identity = None
    if not conflicts and ((anchor and meaningful_text) or (product_id and (item.get("name") or anchor))):
        # The anchor and functional parameters are retained even when IDs are present.
        payload = [IDENTITY_VERSION, host, anchor or final_key, product_id]
        identity = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        level = "STRONG"
        reasons.append("unambiguous_structured_offer" if product_id else "specific_destination_with_page_evidence")
    elif meaningful_text or parser.products:
        level = "SUPPORTING"
        reasons.append("structured_or_textual_context")
    else:
        level = "WEAK"
        reasons.append("no_specific_offer_evidence")

    return {
        "level": level,
        "fingerprint": identity,
        "version": IDENTITY_VERSION,
        "reasons": reasons,
        "conflicts": conflicts,
        "canonical_url": canonical,
        "text": copy,
        "links": links,
        "structured": parser.products[:12],
    }
