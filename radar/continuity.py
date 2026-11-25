"""One-time, conservative reconciliation of legacy CDN fingerprints."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime

from .models import RadarConfig
from .operations import checkpoint_ads
from .pipeline import analyze_ads


def reconcile_saved_media_keys(db) -> int:
    """Link exact historical matches. Existing snapshots, aliases and reviews stay in place."""
    if db.execute("SELECT 1 FROM radar_migrations WHERE name='media_identity_v1'").fetchone():
        return 0
    candidates: dict[str, set[str]] = defaultdict(set)
    rejected: set[str] = set()
    cycles = db.execute("""
        SELECT c.*, (SELECT MAX(run_id) FROM cycle_runs r WHERE r.cycle_id=c.cycle_id) AS run_id
        FROM collection_cycles c WHERE c.status='COMPLETED' AND c.official_plan=1 AND c.finished_at IS NOT NULL
    """).fetchall()
    for cycle in cycles:
        snapshots = [
            json.loads(r[0])
            for r in db.execute("SELECT evidence_json FROM group_history WHERE run_id=?", (cycle["run_id"],))
        ]
        old_groups = [g for g in snapshots if g.get("evidence_level") == "PROBABLE_SAME_OFFER"]
        if not old_groups:
            continue
        keywords = db.execute(
            "SELECT * FROM cycle_keywords WHERE cycle_id=? ORDER BY ordinal",
            (cycle["cycle_id"],),
        ).fetchall()
        if len(keywords) != cycle["total_keywords_expected"] or any(r["status"] != "OK" for r in keywords):
            continue
        records = [(ad, {r["keyword_text"]}) for r in keywords for ad in checkpoint_ads(r)]
        report = analyze_ads(
            records,
            RadarConfig(**json.loads(cycle["config_json"])),
            now=datetime.fromisoformat(cycle["finished_at"]),
        )
        normalized = {tuple(sorted(g.ad_ids)): g for g in report.groups if g.evidence_level == "PROBABLE_SAME_OFFER"}
        for old in old_groups:
            old_id = old["group_id"]
            target = normalized.get(tuple(sorted(old["ad_ids"])))
            if (
                target
                and target.page_id == old["page_id"]
                and target.destination_fingerprints == old.get("destination_fingerprints", [])
                and not target.conflicting_evidence
            ):
                candidates[old_id].add(target.group_id)
            else:
                rejected.add(old_id)
    pairs = [
        (old, next(iter(targets)))
        for old, targets in sorted(candidates.items())
        if len(targets) == 1 and old not in rejected and old not in targets
    ]
    # A legacy ID that is also an observed canonical key must never become an alias.
    canonical = {target for targets in candidates.values() for target in targets}
    pairs = [(old, target) for old, target in pairs if old not in canonical]
    db.executemany("INSERT INTO media_group_aliases VALUES(?,?)", pairs)
    db.execute("INSERT INTO radar_migrations VALUES('media_identity_v1')")
    return len(pairs)
