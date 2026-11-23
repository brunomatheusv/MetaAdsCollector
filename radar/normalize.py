"""Conservative normalization without manufacturing missing evidence."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from meta_ads_collector import Ad

from .models import NormalizedAd

_TRACKING = {"fbclid", "gclid", "src", "ref"}


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
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return value, None, None
        domain = parts.hostname.lower().removeprefix("www.")
        commercial_params = (
            (key, value)
            for key, value in parse_qsl(parts.query)
            if key.lower() not in _TRACKING and not key.lower().startswith("utm_")
        )
        query = urlencode(sorted(commercial_params))
        path = re.sub(r"/+", "/", parts.path).rstrip("/") or "/"
        canonical = urlunsplit((parts.scheme.lower(), domain, path, query, ""))
        return canonical, domain, f"{domain}{path}?{query}".rstrip("?")
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
    distinct_destination_keys = {item[2] for item in destinations if item[2]}
    offer_ambiguous = len(distinct_destination_keys) > 1
    url, domain, destination_key = canonical_url(primary.link_url if primary else None)
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
        offer_ambiguous=offer_ambiguous,
        offer_name=offer_name,
        text_key=_text(combined_text),
        creative_fingerprint=fingerprint,
        stable_ids=stable_ids,
    )
