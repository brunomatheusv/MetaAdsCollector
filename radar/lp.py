"""Opt-in LP investigation and auditable Grouping V2 reconciliation proposals."""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from .lp_evidence import IDENTITY_VERSION, extract_evidence
from .lp_http import LPFetchError, fetch_page, public_url
from .normalize import canonical_url

BLOCKED_HTTP = {401, 403, 429}
RETRYABLE = {"timeout", "transport_error", "http_500", "http_502", "http_503", "http_504"}
SOCIAL = {
    "facebook.com",
    "instagram.com",
    "whatsapp.com",
    "wa.me",
    "messenger.com",
    "linktr.ee",
    "beacons.ai",
    "bio.link",
    "api.whatsapp.com",
}
SHORTENERS = {"fb.me", "bit.ly", "tinyurl.com", "t.co", "cutt.ly", "shorturl.at", "l.facebook.com", "lm.facebook.com"}


@dataclass(frozen=True)
class LPConfig:
    min_ids: int = 3
    ttl_hours: float = 24
    timeout: float = 8
    max_redirects: int = 3
    max_bytes: int = 384 * 1024
    max_urls: int = 40
    retries: int = 1
    global_interval: float = 0.25
    host_interval: float = 1.0

    def __post_init__(self) -> None:
        if not (
            self.min_ids >= 1
            and 0 < self.ttl_hours <= 720
            and 0 < self.timeout <= 30
            and 0 <= self.max_redirects <= 5
            and 0 < self.max_bytes <= 1024 * 1024
            and 1 <= self.max_urls <= 100
            and 0 <= self.retries <= 2
            and 0 <= self.global_interval <= 10
            and 0 <= self.host_interval <= 10
        ):
            raise ValueError("invalid LP settings")


def _fetchable(url: str) -> str | None:
    try:
        safe_url, host, _ = public_url(url)
    except LPFetchError:
        return None
    if host not in SHORTENERS and any(host == site or host.endswith("." + site) for site in SOCIAL):
        return None
    return canonical_url(safe_url)[0]


def _screen(group: dict) -> tuple[str | None, list[str]]:
    if group["final_status"] != "INVESTIGAR":
        return None, ["status_not_investigar"]
    reasons = []
    if group["criterion_max_age"] != "PASSA":
        reasons.append("outside_radar_window")
    if not group["page_id"]:
        reasons.append("missing_page_identity")
    if group["conflicting_evidence"]:
        reasons.append("preexisting_identity_conflict")
    urls = {_fetchable(url) for url in group["destination_urls"]}
    fingerprints = set(group["destination_fingerprints"])
    if len(fingerprints) > 1:
        reasons.append("multiple_destinations")
    if not urls or None in urls:
        reasons.append("missing_or_unsupported_destination")
    if len(urls) > 1:
        # Preserve the HTTP/HTTPS distinction when choosing what to fetch.
        reasons.append("multiple_request_urls")
    if urls and not reasons:
        return next(iter(urls)), []
    return None, reasons


class _Investigator:
    def __init__(self, store, config: LPConfig, fetcher):
        self.store = store
        self.config = config
        self.fetcher = fetcher
        self.results: dict[str, dict] = {}
        self.requests: Counter = Counter()
        self.blocked: set[str] = set()
        self.new_urls: set[str] = set()
        self.cache_hits = 0
        self.shared_hits = 0
        self.last_request: dict[str, float] = {}

    def _rate_limit(self, host: str) -> None:
        if host in self.blocked:
            raise LPFetchError("host_circuit_open")
        now = time.monotonic()
        wait = max(
            self.last_request.get("", 0) + self.config.global_interval - now,
            self.last_request.get(host, 0) + self.config.host_interval - now,
        )
        if wait > 0:
            time.sleep(wait)
        self.last_request[host] = self.last_request[""] = time.monotonic()
        self.requests[host] += 1

    def get(self, url: str) -> tuple[dict | None, str]:
        if url in self.results:
            self.shared_hits += 1
            return self.results[url], "shared"
        cached = self.store.read_lp_cache(url, time.time() - self.config.ttl_hours * 3600, IDENTITY_VERSION)
        if cached is not None:
            self.cache_hits += 1
            self.results[url] = cached
            if cached["http_status"] in BLOCKED_HTTP and cached["final_url"]:
                self.blocked.add(urlsplit(cached["final_url"]).hostname or "")
            return cached, "cache"
        if len(self.new_urls) >= self.config.max_urls:
            return None, "request_budget"
        self.new_urls.add(url)
        result = self._inspect(url)
        self.results[url] = result
        if result["error"] != "host_circuit_open":
            self.store.write_lp_cache(url, result, IDENTITY_VERSION)
            self._cache_final(url, result)
        return result, "inspection"

    def _cache_final(self, url: str, result: dict) -> None:
        final = result["final_url"]
        if not final or result["error"] or result["conflicts"] or final == url:
            return
        if urlsplit(url).hostname == urlsplit(final).hostname and public_url(final)[0] == final:
            cached = {**result, "requested_url": final, "redirect_chain": [final]}
            self.store.write_lp_cache(final, cached, IDENTITY_VERSION, record_history=False)

    def _inspect(self, url: str) -> dict:
        for attempt in range(self.config.retries + 1):
            try:
                response = self.fetcher(
                    url,
                    timeout=self.config.timeout,
                    byte_limit=self.config.max_bytes,
                    max_redirects=self.config.max_redirects,
                    before_request=self._rate_limit,
                )
                try:
                    evidence = extract_evidence(response, url)
                except LPFetchError as exc:
                    raise LPFetchError(
                        str(exc),
                        chain=response.chain or (response.url,),
                        status=response.status,
                        headers=response.headers,
                    ) from exc
                return {
                    **evidence,
                    **_metadata(url, response.chain or (response.url,), response.status, response.headers),
                    "error": None,
                }
            except LPFetchError as exc:
                reason = str(exc)
                chain = exc.chain
                if exc.status in BLOCKED_HTTP or reason == "challenge_or_error_page":
                    self.blocked.add(urlsplit(chain[-1] if chain else url).hostname or "")
                if reason in RETRYABLE and attempt < self.config.retries:
                    time.sleep(min(2, 0.5 * (attempt + 1)))
                    continue
                return {
                    **_metadata(url, chain, exc.status, exc.headers),
                    "level": "UNRESOLVED",
                    "fingerprint": None,
                    "version": IDENTITY_VERSION,
                    "reasons": [reason],
                    "conflicts": [],
                    "canonical_url": None,
                    "error": reason,
                }
        raise AssertionError("validated LP retry count")


def _metadata(url: str, chain: tuple[str, ...], status: int | None, headers: dict[str, str]) -> dict:
    return {
        "requested_url": url,
        "final_url": chain[-1] if chain else None,
        "redirect_chain": list(chain),
        "http_status": status,
        "content_type": headers.get("content-type"),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }


def investigate_groups(
    groups: list[dict], store, *, candidate_min: int = 5, config: LPConfig | None = None, fetcher=fetch_page
) -> dict:
    """Analyze a V2 snapshot. Proposed group/status changes are separate from stored V2 decisions."""
    config = config or LPConfig()
    start = time.monotonic()
    investigator = _Investigator(store, config, fetcher)
    screened = [(group, *_screen(group)) for group in groups]
    page_ids: dict[str, set[str]] = defaultdict(set)
    for group, url, _ in screened:
        if url:
            page_ids[group["page_id"]].update(group["ad_ids"])
    screened.sort(
        key=lambda entry: (
            -len(page_ids.get(entry[0]["page_id"], ())),
            -len(entry[0]["ad_ids"]),
            entry[0]["group_id"],
        )
    )
    cards = {}
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for group, url, skipped in screened:
        group_id = group["group_id"]
        count = len(set(group["ad_ids"]))
        potential = len(page_ids.get(group["page_id"], ()))
        if url and count < config.min_ids and potential < candidate_min:
            skipped.append("below_minimum_without_page_potential")
        eligible = not skipped
        result, source = investigator.get(url) if eligible and url else (None, "skipped")
        card = {
            "group_id": group_id,
            "eligible": eligible,
            "reasons": (["minimum_unique_ids"] if count >= config.min_ids else ["page_consolidation_potential"])
            if eligible
            else [],
            "skipped_reasons": skipped,
            "enrichment_status": result["level"] if result else source.upper(),
            "source": source,
            "lp": result,
            "ad_ids": group["ad_ids"],
            "page_id": group["page_id"],
            "status_before": group["final_status"],
            "status_after": group["final_status"],
            "confidence_before": group["grouping_confidence"],
            "confidence_after": group["grouping_confidence"],
            "grouping_before": [group_id],
            "grouping_after": [group_id],
            "why_changed": [],
        }
        cards[group_id] = card
        if result and result["fingerprint"]:
            buckets[(group["page_id"], result["fingerprint"])].append(card)

    proposals = []
    for (page, fingerprint), bucket in sorted(buckets.items()):
        ids = sorted({ad_id for card in bucket for ad_id in card["ad_ids"]})
        members = sorted(card["group_id"] for card in bucket)
        promoted = len(ids) >= candidate_min
        proposal = {
            "proposal_id": hashlib.sha256(json.dumps([page, fingerprint, members]).encode()).hexdigest()[:12],
            "page_id": page,
            "fingerprint": fingerprint,
            "group_ids": members,
            "ad_ids": ids,
            "status": "CANDIDATO_RADAR" if promoted else "INVESTIGAR",
        }
        proposals.append(proposal)
        for card in bucket:
            card["confidence_after"] = "ALTA"
            card["status_after"] = proposal["status"]
            card["grouping_after"] = members
            card["why_changed"] = ["strong_lp_identity"]
            if len(bucket) > 1:
                card["why_changed"].append("equivalent_strong_identity_same_page")
            if promoted:
                card["why_changed"].append("radar_unique_ids_activity_and_window_pass")

    before = Counter(card["status_before"] for card in cards.values())
    after = Counter(card["status_after"] for card in cards.values())
    for proposal in proposals:
        after[proposal["status"]] -= len(proposal["group_ids"]) - 1
    errors = Counter(r["error"] for r in investigator.results.values() if r["error"])
    levels = Counter(card["enrichment_status"] for card in cards.values() if card["lp"])
    resolved = before["INVESTIGAR"] - after["INVESTIGAR"]
    return {
        "metrics": {
            "groups": len(groups),
            "eligible": sum(card["eligible"] for card in cards.values()),
            "skipped": sum(not card["eligible"] for card in cards.values()),
            "skipped_reasons": dict(Counter(reason for card in cards.values() for reason in card["skipped_reasons"])),
            "budget_deferred": sum(card["source"] == "request_budget" for card in cards.values()),
            "unique_inspections": len(investigator.new_urls),
            "requests_by_host": dict(investigator.requests),
            "cache_hits": investigator.cache_hits,
            "shared_urls_saved": investigator.shared_hits,
            "http_successes": sum(r["http_status"] == 200 for r in investigator.results.values()),
            "errors_by_url": dict(errors),
            "hosts_blocked": sorted(investigator.blocked),
            "levels": dict(levels),
            "confidence_before": dict(Counter(card["confidence_before"] for card in cards.values())),
            "confidence_after": dict(Counter(card["confidence_after"] for card in cards.values())),
            "status_before": dict(before),
            "status_after_proposed": dict(after),
            "identity_confirmed": sum(level == "STRONG" for level in (c["enrichment_status"] for c in cards.values())),
            "review_items_reduced_if_accepted": resolved,
            "review_reduction_pct": round(100 * resolved / before["INVESTIGAR"], 3) if before["INVESTIGAR"] else 0,
            "promotion_proposals": sum(p["status"] == "CANDIDATO_RADAR" for p in proposals),
            "merge_proposals": sum(len(p["group_ids"]) > 1 for p in proposals),
            "splits": 0,
            "duration_seconds": round(time.monotonic() - start, 3),
        },
        "cards": list(cards.values()),
        "proposals": proposals,
    }
