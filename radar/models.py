"""Data contracts for the isolated Radar layer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class RadarConfig:
    country: str = "BR"
    status: str = "active"
    max_results_per_keyword: int = 20
    max_age_days: int = 60
    min_related_ads: int = 5
    batch_size: int = 5
    interval_seconds: float = 1.0
    backoff_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.country.upper() != "BR":
            raise ValueError("Radar MVP supports only country=BR")
        if self.status.lower() != "active":
            raise ValueError("Radar MVP supports only status=active")
        if (
            self.max_results_per_keyword < 1
            or self.max_age_days < 1
            or self.min_related_ads < 1
            or self.batch_size < 1
            or self.interval_seconds < 0
            or self.backoff_seconds < 0
        ):
            raise ValueError("limits must be positive")


@dataclass
class NormalizedAd:
    ad_id: str
    keywords: set[str]
    page_id: str | None
    advertiser: str | None
    is_active: bool | None
    start_time: datetime | None
    age_days: int | None
    destination_url: str | None
    destination_domain: str | None
    destination_key: str | None
    offer_ambiguous: bool
    offer_name: str | None
    text_key: str | None
    creative_fingerprint: str | None
    stable_ids: dict[str, str]
    destinations: tuple[tuple[str, str, str], ...] = ()
    destination_kind: str = "MISSING"
    conflicting_evidence: set[str] = field(default_factory=set)


@dataclass
class EvidenceGroup:
    group_id: str
    keywords: list[str]
    page_id: str | None
    advertiser: str | None
    offer_name: str | None
    total_collected: int
    related_ads_count: int
    unique_creatives_estimate: int | None
    duplicate_creatives_estimate: int | None
    ad_ids: list[str]
    start_dates: list[str | None]
    ages_days: list[int | None]
    first_start_date: str | None
    last_start_date: str | None
    destination_urls: list[str]
    destination_domains: list[str]
    grouping_signals: list[str]
    grouping_confidence: str
    criterion_5_plus: str
    criterion_max_age: str
    final_status: str
    reasons: list[str]
    review_status: str = "NOVO"
    coverage_context: dict[str, Any] | None = None
    freshness_status: str = "ACTIVE_OBSERVED"
    consecutive_full_absences: int = 0
    grouping_version: int = 2
    evidence_level: str = "AMBIGUOUS"
    separation_level: str | None = None
    confidence_reasons: list[str] = field(default_factory=list)
    conflicting_evidence: list[str] = field(default_factory=list)
    noise_flags: list[str] = field(default_factory=list)
    destination_fingerprints: list[str] = field(default_factory=list)
    first_observed_at: str | None = None
    last_observed_at: str | None = None
    legacy_group_ids: list[str] = field(default_factory=list)
    grouping_migration: str | None = None


@dataclass
class RadarReport:
    generated_at: str
    config: dict[str, Any]
    disclaimer: str
    collection: dict[str, Any]
    groups: list[EvidenceGroup] = field(default_factory=list)

    @classmethod
    def create(cls, config: RadarConfig, collection: dict[str, Any], groups: list[EvidenceGroup]) -> RadarReport:
        return cls(
            generated_at=datetime.now(timezone.utc).isoformat(),
            config=asdict(config),
            disclaimer=(
                "CANDIDATO_RADAR significa apenas que as evidencias coletadas atendem aos filtros "
                "de mineracao deste MVP; nao significa oferta vencedora nem validacao comercial."
            ),
            collection=collection,
            groups=groups,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data


@dataclass(frozen=True)
class IncrementalEvent:
    event_type: str
    run_id: int
    group_id: str
    occurred_at: str
    before: dict[str, Any] | None
    after: dict[str, Any]
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReviewQueueItem:
    run_id: int
    group_id: str
    queue_type: str
    priority: int
    created_at: str
    review_status: str
    reason: str
    event_types: list[str]
    group: dict[str, Any]
    acknowledged_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CycleCoverage:
    kind: str
    expected: int
    succeeded: int
    failed: int
    empty: int
    pending: int
    ratio: float
    depth_limit_per_keyword: int
    official_plan: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CollectionCycle:
    cycle_id: str
    started_at: str
    finished_at: str | None
    status: str
    registry_source: str
    registry_version: str
    registry_hash: str
    plan_hash: str
    config: dict[str, Any]
    total_keywords_expected: int
    attempted: int
    succeeded: int
    failed: int
    empty: int
    pending: int
    coverage_ratio: float
    coverage_kind: str
    official_plan: bool
    run_ids: list[int]
    reason: str | None = None
    duration_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
