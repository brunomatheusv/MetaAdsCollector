"""Evidence-backed commercial review, separate from the technical Radar filters."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .normalize import canonical_url, normalize_ad
from .operations import checkpoint_ads
from .shortlist import _destination_link, _text

DEFAULT_CEILING_BRL = 47
PRICE_PRIORITY_POINTS = {"MAXIMA": 3, "ALTA": 2, "MEDIA": 1, "SECUNDARIA": 0}


def price_priority(price: float) -> str:
    if price < 10:
        return "SECUNDARIA"
    if price <= 27.90:
        return "MAXIMA"
    return "ALTA" if price <= 37 else "MEDIA"


CATEGORIES = {
    "INFOPRODUTO_LOW_TICKET_BR",
    "SOFTWARE_SAAS",
    "PRODUTO_FISICO",
    "SERVICO",
    "REFERENCIA_INTERNACIONAL",
    "INDETERMINADO",
}
FIELDS = (
    "name",
    "niche",
    "subniche",
    "audience",
    "pain",
    "format",
    "promise",
    "checkout",
    "price",
    "currency",
    "price_verified",
    "market",
)


def _http(url) -> bool:
    return isinstance(url, str) and canonical_url(url)[2] is not None


def _reviews(workspace: Path, cycle_id: str) -> dict:
    path = workspace / "qualificacao-comercial.json"
    if not path.is_file():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("qualificacao-comercial.json deve conter uma lista")
    selected = {}
    for row in rows:
        if row.get("cycle_id") != cycle_id:
            continue
        if (
            row["group_id"] in selected
            or row["category"] not in CATEGORIES
            or not _http(row["lp"])
            or not row["sources"]
            or not all(_http(u) for u in row["sources"])
            or (row.get("checkout") is not None and not _http(row["checkout"]))
        ):
            raise ValueError("evidência comercial inválida ou duplicada")
        datetime.fromisoformat(row["verified_at"])
        selected[row["group_id"]] = row
    return selected


def _verified_creatives(store, cycle, rows):
    targets = {ad_id for row in rows for ad_id in row["ad_ids"]}
    signatures: dict[str, set] = {}
    now = datetime.fromisoformat(cycle.finished_at or cycle.started_at)
    if targets:
        for keyword in store.cycle_keywords(cycle.cycle_id):
            if keyword["status"] != "OK":
                continue
            for ad in checkpoint_ads(keyword):
                ad_id = str(ad.id)
                if ad_id in targets:
                    signatures.setdefault(ad_id, set()).add(normalize_ad(ad, set(), now).creative_fingerprint)
    return signatures


def classify(workspace, store, cycle, technical: list[dict], *, automatic=None, ceiling=None) -> list[dict]:
    reviews = _reviews(workspace, cycle.cycle_id)
    automatic = automatic or {}
    ceiling = DEFAULT_CEILING_BRL if ceiling is None else ceiling
    signatures = _verified_creatives(store, cycle, technical)
    previous = store.prior_full_group_counts(cycle)
    results = []
    for row in technical:
        review = reviews.get(row["group_id"], {})
        if review and (
            str(review["page_id"]) != row["page_id"]
            or not any(canonical_url(review["lp"])[2] == canonical_url(url)[2] for url in row["destinations"])
        ):
            review = {}
        review = review or automatic.get(row["group_id"], {})
        category = review.get("category", "INDETERMINADO")
        fields = {key: review.get(key) for key in FIELDS}
        price = fields["price"]
        verified = (
            category == "INFOPRODUTO_LOW_TICKET_BR"
            and fields["market"] == "BR"
            and fields["currency"] == "BRL"
            and fields["price_verified"] is True
            and type(price) in (int, float)
            and 0 < price < float("inf")
            and price <= ceiling
            and all(fields[key] for key in ("name", "niche", "format", "promise", "checkout"))
            and any(canonical_url(url)[2] == canonical_url(fields["checkout"])[2] for url in review["sources"])
        )
        if category == "INFOPRODUTO_LOW_TICKET_BR" and not verified:
            category = "INDETERMINADO"
        stable_fingerprints = [
            next(iter(found))
            for ad in row["ad_ids"]
            if (found := signatures.get(ad)) and len(found) == 1 and None not in found
        ]
        earlier = previous.get(row["group_id"])
        results.append(
            {
                **row,
                **fields,
                "category": category,
                "qualified": verified,
                "lp": review.get("lp"),
                "sources": review.get("sources", []),
                "verified_at": review.get("verified_at"),
                "scope": (
                    "ACIMA_DO_TETO"
                    if fields["market"] == "BR"
                    and fields["currency"] == "BRL"
                    and fields["price_verified"] is True
                    and type(price) in (int, float)
                    and price > ceiling
                    else "VERIFICADO"
                    if verified
                    else "EM_INVESTIGACAO"
                    if category == "INDETERMINADO"
                    else "FORA_DO_ESCOPO"
                ),
                "evidence_source": review.get("evidence_source") or ("reviewed" if review.get("sources") else None),
                "qualification_reason": review.get("reason")
                if review
                else "Sem evidência comercial verificada neste ciclo.",
                "verifiable_creative_fingerprints": len(set(stable_fingerprints)),
                "ads_with_verifiable_creative": len(stable_fingerprints),
                "prior_cycle_ads": earlier,
                "ads_growth": row["observed_ads"] - earlier if earlier is not None else None,
            }
        )
    for row in results:
        band = None
        if row["qualified"]:
            band = price_priority(row["price"])
        row["price_priority"] = band
        points = (
            {
                "identidade e formato comprovados": 2,
                "preço front-end corroborado": 2,
                "faixa de preço": PRICE_PRIORITY_POINTS[band],
                "10+ anúncios ativos": int(row["observed_ads"] >= 10),
                "diversidade de criativos": int(row["verifiable_creative_fingerprints"] >= 2),
                "crescimento positivo": int((row["ads_growth"] or 0) > 0),
            }
            if row["qualified"]
            else {}
        )
        row["priority"] = sum(points.values()) if points else None
        row["priority_basis"] = points
        row["missing_information"] = [
            name
            for field, name in (
                ("subniche", "subnicho"),
                ("audience", "público"),
                ("pain", "dor"),
                ("price", "preço"),
                ("checkout", "checkout"),
            )
            if row[field] is None
        ]
        row["inferences"] = []
        row["risks"] = ["Anúncios e crescimento publicitário não comprovam vendas, demanda ou lucro."]
        row["next_step"] = (
            "Avaliar aderência ao público e testar demanda de forma independente."
            if row["qualified"]
            else (
                "Verificar os dados comerciais faltantes na LP e no checkout."
                if row["scope"] == "EM_INVESTIGACAO"
                else "Manter no histórico comercial fora da shortlist brasileira."
            )
        )
    return results


def render_commercial(rows: list[dict], cycle, *, shortlist: bool = False) -> str:
    lines = ["# Oportunidades brasileiras qualificadas" if shortlist else "# Qualificação comercial dos candidatos", ""]
    lines.extend([f"Ciclo `{cycle.cycle_id}` · **{cycle.coverage_kind}**", ""])
    if cycle.coverage_kind != "FULL_COVERAGE":
        lines.extend(["**Resultado provisório devido à cobertura parcial.**", ""])
    if not rows:
        lines.append("Nenhum infoproduto low-ticket brasileiro foi verificado neste ciclo.")
    for row in rows:
        title = row["name"] or row["offer_name"] or row["group_id"]
        growth = row["ads_growth"]
        delta = growth if growth is not None else "sem base comparável"
        price = "Não verificado" if row["price"] is None else f"{row['currency']} {row['price']}"
        lines.extend(
            [
                f"## {_text(title)}",
                "",
                f"- Categoria: {_text(row['category'])} · anunciante {_text(row['advertiser'])} "
                f"· grupo `{row['group_id']}`",
                f"- Nicho: {_text(row['niche'])} · formato: {_text(row['format'])} · preço: {_text(price)}",
                f"- Subnicho: {_text(row['subniche'])} · público: {_text(row['audience'])} · dor: {_text(row['pain'])}",
                f"- Promessa declarada: {_text(row['promise'])}",
                f"- Anúncios: {row['observed_ads']} · fingerprints distintos verificados: "
                f"{row['verifiable_creative_fingerprints']} em {row['ads_with_verifiable_creative']} IDs.",
                f"- Início dos anúncios: {_text(row['first_start_date'])} a {_text(row['last_start_date'])}. "
                f"Variação em relação ao ciclo FULL anterior: {delta}.",
                f"- Tempo desde o início do anúncio mais antigo: {_text(str(row['oldest_ad_age_days']))} dias.",
                f"- Parecer: {_text(row['qualification_reason'])}",
                f"- Informações faltantes: {_text(', '.join(row['missing_information']))}",
                f"- Risco: {_text(row['risks'][0])}",
                f"- Próximo passo: {_text(row['next_step'])}",
            ]
        )
        if row["priority"] is not None:
            basis = ", ".join(f"{name} +{points}" for name, points in row["priority_basis"].items())
            lines.append(f"- Prioridade: {row['priority']}/10 · faixa {row['price_priority']} · {_text(basis)}")
        for label, url in (("LP", row["lp"]), ("Checkout", row["checkout"])):
            if url and (safe := _destination_link(url)):
                lines.append(f"- [{label}](<{safe}>)")
        if row["ad_links"]:
            lines.append("- " + " · ".join(f"[Anúncio {i}](<{url}>)" for i, url in enumerate(row["ad_links"], 1)))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
