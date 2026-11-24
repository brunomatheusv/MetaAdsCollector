"""Conservative normalization without manufacturing missing evidence."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from meta_ads_collector import Ad

from .models import NormalizedAd

_TRACKING = {"fbclid", "gclid", "dclid", "msclkid", "gbraid", "wbraid", "ttclid"}
_GENERIC_HOSTS = {
    "facebook.com", "instagram.com", "whatsapp.com", "wa.me", "messenger.com",
    "bit.ly", "tinyurl.com", "t.co", "linktr.ee", "beacons.ai", "bio.link", "cutt.ly", "shorturl.at",
}
_GENERIC_PATHS = {"/", "/home", "/shop", "/store", "/catalog", "/catalogo", "/products", "/collections",
                  "/loja", "/checkout", "/cart", "/login", "/search", "/contact", "/contato", "/redirect", "/go", "/r"}


def destination_kind(domain: str | None, key: str | None) -> str:
    if domain is None or key is None:
        return "MISSING"
    if any(domain == host or domain.endswith("." + host) for host in _GENERIC_HOSTS):
        return "PLATFORM_OR_REDIRECT"
    path = "/" + key.split("/", 1)[1].split("?", 1)[0].lower()
    if path.startswith(("/redirect/", "/go/", "/r/")):
        return "PLATFORM_OR_REDIRECT"
    return "GENERIC_PATH" if path in _GENERIC_PATHS else "SPECIFIC_PATH"



def _text(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", value.lower(), flags=re.UNICODE)).strip()
    return normalized or None


def canonical_url(value: str | None) -> tuple[str | None, str | None, str | None]:
    if not value:
        return None, None, None
    try:
        parts = urlsplit(value.strip())
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None:
            return value, None, None
        domain = parts.hostname.lower().removeprefix("www.")
        netloc = f"[{domain}]" if ":" in domain else domain
        port = parts.port
        if port is not None and (parts.scheme, port) not in {("http", 80), ("https", 443)}:
            netloc = f"{netloc}:{port}"
        commercial_params = (
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in _TRACKING and not key.lower().startswith("utm_")
        )
        # Preserve the order of repeated values, which can affect server routing.
        query = urlencode(sorted(commercial_params, key=lambda item: item[0]))
        path = parts.path.removesuffix("/") if not parts.path.endswith("//") else parts.path
        path = path or "/"
        canonical = urlunsplit((parts.scheme, netloc, path, query, ""))
        return canonical, domain, urlunsplit(("", netloc, path, query, "")).removeprefix("//")
    except (TypeError, ValueError):
        return value, None, None


def _creative_values(ad: Ad) -> list[str]:
    values: list[str] = []
    for creative in ad.creatives:
        for name in ("body", "title", "description", "caption", "image_url", "video_url", "thumbnail_url"):
            value = getattr(creative, name, None)
            if value:
                values.append(str(value).strip())
    return values


def normalize_ad(ad: Ad, keywords: set[str], now: datetime) -> NormalizedAd:
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    start = ad.delivery_start_time
    if start and start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    age = (now - start).days if start else None
    primary = ad.creatives[0] if ad.creatives else None
    destinations = [canonical_url(creative.link_url) for creative in ad.creatives if creative.link_url]
    valid_destinations = sorted({(url, domain, key) for url, domain, key in destinations if url and domain and key})
    keys = {key for _, _, key in valid_destinations}
    conflicts = set()
    if len(keys) > 1:
        conflicts.add("multiplos_destinos_no_mesmo_anuncio")
    if destinations and any(key is None for _, _, key in destinations):
        conflicts.add("destino_invalido_ou_nao_interpretavel")
    url, domain, destination_key = valid_destinations[0] if valid_destinations else (None, None, None)
    offer_name = (primary.title or primary.caption) if primary else None
    combined_text = " ".join(
        value
        for value in (
            primary.title if primary else None,
            primary.caption if primary else None,
            primary.body if primary else None,
        )
        if value
    )
    creative_values = _creative_values(ad)
    fingerprint = None
    if creative_values:
        serialized = json.dumps(creative_values, ensure_ascii=False, sort_keys=True).encode()
        fingerprint = hashlib.sha256(serialized).hexdigest()[:20]
    stable_ids = {}
    for key, value in (("collation_id", ad.collation_id), ("ad_library_id", ad.ad_library_id), ("ad_id", ad.ad_id)):
        if value:
            stable_ids[key] = str(value)
    return NormalizedAd(
        ad_id=str(ad.id),
        keywords=set(keywords),
        page_id=ad.page.id if ad.page else None,
        advertiser=ad.page.name if ad.page else None,
        is_active=ad.is_active,
        start_time=start,
        age_days=age,
        destination_url=url,
        destination_domain=domain,
        destination_key=destination_key,
        offer_ambiguous=bool(conflicts),
        offer_name=offer_name,
        text_key=_text(combined_text),
        creative_fingerprint=fingerprint,
        stable_ids=stable_ids,
        destinations=tuple(valid_destinations),
        destination_kind=destination_kind(domain, destination_key),
        conflicting_evidence=conflicts,
    )
