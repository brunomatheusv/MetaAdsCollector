"""SQLite persistence for runs, observations, group history, and human review."""

from __future__ import annotations

import json
import pickle
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path

from .models import CollectionCycle, EvidenceGroup, IncrementalEvent, RadarReport, ReviewQueueItem
from .normalize import canonical_url

REVIEW_STATUSES = {"NOVO", "REVISAR", "APROVADO", "DESCARTADO", "ACOMPANHAR"}


class RadarStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(str(self.path))
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def _initialize(self) -> None:
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS lp_fetch_cache (
                    requested_url TEXT NOT NULL, identity_version INTEGER NOT NULL,
                    fetched_epoch REAL NOT NULL, result_json TEXT NOT NULL,
                    PRIMARY KEY(requested_url, identity_version)
                );
                CREATE TABLE IF NOT EXISTS lp_fetch_history (
                    requested_url TEXT NOT NULL, identity_version INTEGER NOT NULL,
                    fetched_epoch REAL NOT NULL, result_json TEXT NOT NULL,
                    PRIMARY KEY(requested_url, identity_version, fetched_epoch)
                );
                CREATE TABLE IF NOT EXISTS batch_payloads (
                    run_id INTEGER NOT NULL, keyword_id TEXT NOT NULL, payload BLOB NOT NULL,
                    PRIMARY KEY(run_id, keyword_id), FOREIGN KEY(run_id) REFERENCES runs(id)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                    status TEXT NOT NULL, config_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS keyword_results (
                    run_id INTEGER NOT NULL, keyword_id TEXT NOT NULL, keyword_text TEXT NOT NULL,
                    source TEXT NOT NULL, status TEXT NOT NULL, raw_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT, PRIMARY KEY(run_id, keyword_id), FOREIGN KEY(run_id) REFERENCES runs(id)
                );
                CREATE TABLE IF NOT EXISTS ad_observations (
                    run_id INTEGER NOT NULL, ad_id TEXT NOT NULL, keyword_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL, PRIMARY KEY(run_id, ad_id, keyword_id),
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );
                CREATE TABLE IF NOT EXISTS groups (
                    group_id TEXT PRIMARY KEY, review_status TEXT NOT NULL DEFAULT 'NOVO',
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS group_history (
                    run_id INTEGER NOT NULL, group_id TEXT NOT NULL, observed_at TEXT NOT NULL,
                    related_ads_count INTEGER NOT NULL, final_status TEXT NOT NULL,
                    evidence_json TEXT NOT NULL, PRIMARY KEY(run_id, group_id),
                    FOREIGN KEY(run_id) REFERENCES runs(id), FOREIGN KEY(group_id) REFERENCES groups(group_id)
                );
                CREATE TABLE IF NOT EXISTS grouping_aliases (
                    legacy_group_id TEXT PRIMARY KEY REFERENCES groups(group_id),
                    group_id TEXT NOT NULL UNIQUE REFERENCES groups(group_id)
                );
                CREATE TABLE IF NOT EXISTS radar_migrations (name TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS media_group_aliases (
                    legacy_id TEXT PRIMARY KEY REFERENCES groups(group_id), canonical_id TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS media_group_aliases_target ON media_group_aliases(canonical_id);
                CREATE TABLE IF NOT EXISTS incremental_events (
                    run_id INTEGER NOT NULL, group_id TEXT NOT NULL, event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL, before_json TEXT, after_json TEXT NOT NULL,
                    reasons_json TEXT NOT NULL,
                    PRIMARY KEY(run_id, group_id, event_type),
                    FOREIGN KEY(run_id) REFERENCES runs(id), FOREIGN KEY(group_id) REFERENCES groups(group_id)
                );
                CREATE TABLE IF NOT EXISTS review_queue (
                    run_id INTEGER NOT NULL, group_id TEXT NOT NULL, queue_type TEXT NOT NULL,
                    priority INTEGER NOT NULL, created_at TEXT NOT NULL, reason TEXT NOT NULL,
                    event_types_json TEXT NOT NULL, group_json TEXT NOT NULL, acknowledged_at TEXT,
                    PRIMARY KEY(run_id, group_id),
                    FOREIGN KEY(run_id) REFERENCES runs(id), FOREIGN KEY(group_id) REFERENCES groups(group_id)
                );
                CREATE TABLE IF NOT EXISTS collection_cycles (
                    cycle_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                    status TEXT NOT NULL, registry_source TEXT NOT NULL, registry_version TEXT NOT NULL,
                    registry_hash TEXT NOT NULL, plan_hash TEXT NOT NULL, config_json TEXT NOT NULL,
                    total_keywords_expected INTEGER NOT NULL, official_plan INTEGER NOT NULL,
                    reason TEXT
                );
                CREATE TABLE IF NOT EXISTS managed_cycles (
                    cycle_id TEXT PRIMARY KEY REFERENCES collection_cycles(cycle_id)
                );
                CREATE TABLE IF NOT EXISTS cycle_keywords (
                    cycle_id TEXT NOT NULL, ordinal INTEGER NOT NULL, keyword_id TEXT NOT NULL,
                    keyword_text TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
                    raw_count INTEGER NOT NULL DEFAULT 0, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    payload BLOB, updated_at TEXT,
                    PRIMARY KEY(cycle_id, keyword_id),
                    FOREIGN KEY(cycle_id) REFERENCES collection_cycles(cycle_id)
                );
                CREATE TABLE IF NOT EXISTS cycle_runs (
                    cycle_id TEXT NOT NULL, run_id INTEGER NOT NULL,
                    PRIMARY KEY(cycle_id,run_id), FOREIGN KEY(cycle_id) REFERENCES collection_cycles(cycle_id),
                    FOREIGN KEY(run_id) REFERENCES runs(id)
                );
                CREATE TABLE IF NOT EXISTS group_freshness (
                    group_id TEXT PRIMARY KEY, last_seen_cycle TEXT, last_seen_at TEXT,
                    consecutive_full_absences INTEGER NOT NULL DEFAULT 0,
                    freshness_status TEXT NOT NULL DEFAULT 'ACTIVE_OBSERVED',
                    FOREIGN KEY(group_id) REFERENCES groups(group_id)
                );
            """)

    def ensure_media_identity(self) -> int:
        from .continuity import reconcile_saved_media_keys

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return reconcile_saved_media_keys(db)

    def create_cycle(self, cycle: CollectionCycle, keywords: list[tuple[str, str, str]], *,
                     managed: bool = False) -> None:
        with self.connect() as db:
            db.execute("""INSERT INTO collection_cycles
                (cycle_id,started_at,finished_at,status,registry_source,registry_version,registry_hash,
                 plan_hash,config_json,total_keywords_expected,official_plan,reason)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (
                cycle.cycle_id, cycle.started_at, cycle.finished_at, cycle.status, cycle.registry_source,
                cycle.registry_version, cycle.registry_hash, cycle.plan_hash,
                json.dumps(cycle.config, ensure_ascii=False, sort_keys=True), cycle.total_keywords_expected,
                int(cycle.official_plan), cycle.reason,
            ))
            db.executemany("""INSERT INTO cycle_keywords
                (cycle_id,ordinal,keyword_id,keyword_text,source) VALUES(?,?,?,?,?)""",
                [(cycle.cycle_id, i, keyword_id, text, source)
                 for i, (keyword_id, text, source) in enumerate(keywords)])

            if managed:
                db.execute("INSERT INTO managed_cycles VALUES(?)", (cycle.cycle_id,))

    def latest_managed_cycle(self) -> str | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT c.cycle_id FROM managed_cycles m JOIN collection_cycles c USING(cycle_id)
                   ORDER BY c.started_at DESC, c.rowid DESC LIMIT 1"""
            ).fetchone()
        return row[0] if row else None

    def cycle_row(self, cycle_id: str) -> sqlite3.Row:
        with self.connect() as db:
            row = db.execute("SELECT * FROM collection_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
        if row is None:
            raise KeyError(cycle_id)
        return row

    def cycle_keywords(self, cycle_id: str) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                "SELECT * FROM cycle_keywords WHERE cycle_id=? ORDER BY ordinal", (cycle_id,)
            ).fetchall()

    def attach_cycle_run(self, cycle_id: str, run_id: int) -> None:
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO cycle_runs VALUES(?,?)", (cycle_id, run_id))

    def record_cycle_keyword(self, cycle_id: str, keyword_id: str, status: str, raw_count: int = 0,
                             error: str | None = None, payload: bytes | None = None) -> None:
        with self.connect() as db:
            db.execute("""UPDATE cycle_keywords SET status=?,raw_count=?,error=?,payload=?,
                attempts=attempts+1,updated_at=? WHERE cycle_id=? AND keyword_id=?""", (
                status, raw_count, error, payload, datetime.now(timezone.utc).isoformat(), cycle_id, keyword_id,
            ))

    def update_cycle(self, cycle_id: str, status: str, *, finished: bool, reason: str | None = None) -> None:
        with self.connect() as db:
            db.execute("UPDATE collection_cycles SET status=?,finished_at=?,reason=? WHERE cycle_id=?", (
                status, datetime.now(timezone.utc).isoformat() if finished else None, reason, cycle_id,
            ))

    def begin_cycle_run(self, cycle_id: str, config: dict) -> tuple[int, int]:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            recovered = db.execute(
                """UPDATE runs SET status='INTERRUPTED', finished_at=?
                   WHERE status='RUNNING' AND id IN (SELECT run_id FROM cycle_runs WHERE cycle_id=?)""",
                (now, cycle_id),
            ).rowcount
            run_id = db.execute(
                "INSERT INTO runs(started_at,status,config_json) VALUES(?,'RUNNING',?)",
                (now, json.dumps({**config, "cycle_id": cycle_id}, sort_keys=True)),
            ).lastrowid
            db.execute("INSERT INTO cycle_runs VALUES(?,?)", (cycle_id, run_id))
            db.execute(
                "UPDATE collection_cycles SET status='RUNNING', finished_at=NULL, reason=NULL WHERE cycle_id=?",
                (cycle_id,),
            )
        return int(run_id), recovered

    def checkpoint_search(self, run_id, entry, ads, *, cycle_id=None, error=None):
        now = datetime.now(timezone.utc).isoformat()
        status = "OK" if error is None else "ERROR"
        count = len(ads)
        payload = pickle.dumps(ads) if error is None else None
        with self.connect() as db:
            if cycle_id is not None:
                updated = db.execute(
                    """UPDATE cycle_keywords SET status=?,raw_count=?,error=?,payload=?,
                       attempts=attempts+1,updated_at=? WHERE cycle_id=? AND keyword_id=? AND status<>'OK'""",
                    (status, count, error, payload, now, cycle_id, entry.id),
                )
                if updated.rowcount != 1:
                    raise ValueError("inconsistent keyword checkpoint")
            elif payload is not None:
                db.execute("INSERT INTO batch_payloads VALUES(?,?,?)", (run_id, entry.id, payload))
            db.execute(
                """INSERT INTO keyword_results VALUES(?,?,?,?,?,?,?) ON CONFLICT(run_id,keyword_id)
                   DO UPDATE SET status=excluded.status,raw_count=excluded.raw_count,error=excluded.error""",
                (run_id, entry.id, entry.text, entry.source, status, count, error),
            )
            db.executemany(
                "INSERT OR IGNORE INTO ad_observations VALUES(?,?,?,?)",
                [(run_id, str(ad.id), entry.id, now) for ad in ads],
            )

    def batch_keywords(self, run_id: int) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                """SELECT k.*, p.payload FROM keyword_results k LEFT JOIN batch_payloads p
                   USING(run_id,keyword_id) WHERE k.run_id=? AND k.status='OK' ORDER BY k.rowid""",
                (run_id,),
            ).fetchall()

    def finalize_batch(self, run_id: int, report: RadarReport, *, incomplete: bool) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.persist_report(run_id, report, connection=db)
            db.execute("UPDATE runs SET status=?,finished_at=? WHERE id=?", (
                "COMPLETED_WITH_ERRORS" if incomplete else "COMPLETED",
                datetime.now(timezone.utc).isoformat(), run_id,
            ))

    def finalize_cycle(self, cycle_id, run_id, report, *, full, unfinished, status, reason):
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.persist_report(run_id, report, connection=db)
            self.reconcile_cycle(cycle_id, run_id, {group.group_id for group in report.groups},
                                 full=full, connection=db)
            run_status = "COMPLETED_WITH_ERRORS" if unfinished else "COMPLETED"
            db.execute("UPDATE runs SET status=?,finished_at=? WHERE id=?", (run_status, now, run_id))
            db.execute(
                "UPDATE collection_cycles SET status=?,finished_at=?,reason=? WHERE cycle_id=?",
                (status, None if unfinished else now, reason, cycle_id),
            )

    def cycle_run_ids(self, cycle_id: str) -> list[int]:
        with self.connect() as db:
            rows = db.execute("SELECT run_id FROM cycle_runs WHERE cycle_id=? ORDER BY run_id", (cycle_id,)).fetchall()
        return [int(row[0]) for row in rows]

    def start_run(self, config: dict) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            cur = db.execute(
                "INSERT INTO runs(started_at,status,config_json) VALUES(?, 'RUNNING', ?)",
                (now, json.dumps(config, ensure_ascii=False, sort_keys=True)),
            )
            return int(cur.lastrowid)

    def run_row(self, run_id: int) -> sqlite3.Row:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return row

    def finish_run(self, run_id: int, status: str = "COMPLETED") -> None:
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET finished_at=?, status=? WHERE id=?",
                (datetime.now(timezone.utc).isoformat(), status, run_id),
            )

    def keyword_done(self, run_id: int, keyword_id: str) -> bool:
        with self.connect() as db:
            row = db.execute(
                "SELECT status FROM keyword_results WHERE run_id=? AND keyword_id=?", (run_id, keyword_id)
            ).fetchone()
        return bool(row and row["status"] == "OK")

    def record_keyword(self, run_id: int, keyword_id: str, text: str, source: str, status: str,
                       raw_count: int = 0, error: str | None = None) -> None:
        with self.connect() as db:
            db.execute("""
                INSERT INTO keyword_results(run_id,keyword_id,keyword_text,source,status,raw_count,error)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(run_id,keyword_id) DO UPDATE SET
                status=excluded.status, raw_count=excluded.raw_count, error=excluded.error
            """, (run_id, keyword_id, text, source, status, raw_count, error))

    def record_ad_origin(self, run_id: int, ad_id: str, keyword_id: str) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO ad_observations VALUES(?,?,?,?)",
                (run_id, ad_id, keyword_id, datetime.now(timezone.utc).isoformat()),
            )

    @staticmethod
    def _grouping_migrations(db: sqlite3.Connection, report: RadarReport) -> dict[str, str]:
        # Index legacy history by ad ID. Historical membership prevents silent
        # transfer when only one branch of an old split is visible in this run.
        legacy_members: dict[str, set[str]] = {}
        legacy_pages: dict[str, set[str | None]] = {}
        legacy_destinations: dict[str, set[str | None]] = {}
        by_ad: dict[str, set[str]] = {}
        for row in db.execute("""
            SELECT h.group_id, h.evidence_json FROM group_history h
            WHERE NOT EXISTS (SELECT 1 FROM grouping_aliases a WHERE a.legacy_group_id=h.group_id)
              AND json_extract(h.evidence_json, '$.grouping_version') IS NULL
        """):
            evidence = json.loads(row["evidence_json"])
            legacy_id = row["group_id"]
            legacy_pages.setdefault(legacy_id, set()).add(evidence.get("page_id"))
            destinations = {canonical_url(url)[2] for url in evidence.get("destination_urls", [])}
            legacy_destinations.setdefault(legacy_id, set()).update(destinations or {None})
            for ad_id in evidence.get("ad_ids", []):
                legacy_members.setdefault(legacy_id, set()).add(ad_id)
                by_ad.setdefault(ad_id, set()).add(legacy_id)
        migrations = {}
        for group in report.groups:
            aliases = db.execute(
                "SELECT legacy_group_id FROM grouping_aliases WHERE group_id=?", (group.group_id,)
            ).fetchall()
            ids = set(group.ad_ids)
            overlap = set().union(*(by_ad.get(ad_id, set()) for ad_id in ids))
            group.legacy_group_ids = sorted(overlap | {row[0] for row in aliases})
            if aliases:
                group.grouping_migration = "MATCHED" if not overlap else "AMBIGUOUS"
            elif overlap:
                group.grouping_migration = "AMBIGUOUS"
                if len(overlap) == 1 and group.evidence_level == "STRONG_SAME_OFFER":
                    reviewed = db.execute(
                        "SELECT review_status FROM groups WHERE group_id=? AND review_status<>'NOVO'",
                        (group.group_id,),
                    ).fetchone()
                    old_id = next(iter(overlap))
                    if (
                        not reviewed and ids == legacy_members[old_id] and {group.page_id} == legacy_pages[old_id]
                        and set(group.destination_fingerprints) == legacy_destinations[old_id]
                    ):
                        group.grouping_migration = "MATCHED"
                        migrations[group.group_id] = old_id
            if group.grouping_migration == "AMBIGUOUS":
                group.noise_flags.append("historico_v1_sem_correspondencia_inequivoca")
                group.reasons.append("historico V1 exige conciliacao humana")
        return migrations

    @staticmethod
    def _observe_group(
        db: sqlite3.Connection, group: EvidenceGroup, now: str, legacy_id: str | None
    ) -> None:
        if legacy_id:
            db.execute("""
                INSERT OR IGNORE INTO groups(group_id,review_status,first_seen,last_seen)
                SELECT ?,review_status,first_seen,? FROM groups WHERE group_id=?
            """, (group.group_id, now, legacy_id))
            db.execute("""
                UPDATE groups SET (review_status,first_seen) =
                    (SELECT review_status,first_seen FROM groups WHERE group_id=?)
                WHERE group_id=? AND review_status='NOVO'
            """, (legacy_id, group.group_id))
            db.execute("INSERT INTO grouping_aliases VALUES(?,?)", (legacy_id, group.group_id))
        db.execute("""
            INSERT INTO groups(group_id,review_status,first_seen,last_seen) VALUES(?, 'NOVO', ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET last_seen=excluded.last_seen
        """, (group.group_id, now, now))
        row = db.execute(
            "SELECT review_status,first_seen,last_seen FROM groups WHERE group_id=?", (group.group_id,)
        ).fetchone()
        group.review_status = row["review_status"]
        group.first_observed_at = row["first_seen"]
        group.last_observed_at = row["last_seen"]

    @staticmethod
    def _prior_creative_observations(db: sqlite3.Connection, run_id: int) -> dict[tuple, dict]:
        rows = db.execute("""
            SELECT evidence_json FROM group_history
            WHERE run_id=(SELECT MAX(run_id) FROM group_history WHERE run_id<?)
              AND json_extract(evidence_json, '$.evidence_level')='PROBABLE_SAME_OFFER'
        """, (run_id,))
        return {
            (g["page_id"], tuple(g["ad_ids"]), tuple(g.get("destination_fingerprints", []))): g
            for row in rows if (g := json.loads(row[0]))["page_id"]
        }

    def persist_report(self, run_id: int, report: RadarReport, *,
                       connection: sqlite3.Connection | None = None) -> list[IncrementalEvent]:
        now = datetime.now(timezone.utc).isoformat()
        events: list[IncrementalEvent] = []
        with (nullcontext(connection) if connection is not None else self.connect()) as db:
            if connection is None:
                db.execute("BEGIN IMMEDIATE")
            migrations = self._grouping_migrations(db, report)
            prior_observations = self._prior_creative_observations(db, run_id)
            for group in report.groups:
                legacy_id = migrations.get(group.group_id)
                media_aliases = db.execute("""
                    SELECT g.group_id,g.review_status,g.first_seen FROM groups g
                    JOIN media_group_aliases m ON m.legacy_id=g.group_id WHERE m.canonical_id=?
                """, (group.group_id,)).fetchall()
                group.legacy_group_ids = sorted(set(group.legacy_group_ids) | {r["group_id"] for r in media_aliases})
                first_group = not db.execute("SELECT 1 FROM groups WHERE group_id=?", (group.group_id,)).fetchone()
                previous_row = db.execute("""
                    SELECT gh.evidence_json FROM group_history gh
                    WHERE (gh.group_id IN (?, ?) OR gh.group_id IN (
                        SELECT legacy_id FROM media_group_aliases WHERE canonical_id=?
                    )) AND gh.run_id < ? ORDER BY gh.run_id DESC, gh.group_id LIMIT 1
                """, (group.group_id, legacy_id, group.group_id, run_id)).fetchone()
                previous = json.loads(previous_row[0]) if previous_row else None
                self._observe_group(db, group, now, legacy_id)
                if media_aliases and first_group and legacy_id is None:
                    decisions = {r["review_status"] for r in media_aliases} - {"NOVO"}
                    if len(decisions) > 1:
                        group.noise_flags.append("decisoes_humanas_historicas_conflitantes")
                    group.review_status = next(iter(decisions)) if len(decisions) == 1 else "NOVO"
                    group.first_observed_at = min(r["first_seen"] for r in media_aliases)
                    db.execute("UPDATE groups SET review_status=?,first_seen=? WHERE group_id=?",
                               (group.review_status, group.first_observed_at, group.group_id))
                current = dict(group.__dict__)
                db.execute("""
                    INSERT OR REPLACE INTO group_history
                    (run_id,group_id,observed_at,related_ads_count,final_status,evidence_json)
                    VALUES(?,?,?,?,?,?)
                """, (run_id, group.group_id, now, group.related_ads_count, group.final_status,
                      json.dumps(current, ensure_ascii=False, sort_keys=True)))
                if (
                    previous is None and group.evidence_level == "PROBABLE_SAME_OFFER"
                    and not group.conflicting_evidence
                ):
                    lookup = (group.page_id, tuple(group.ad_ids), tuple(group.destination_fingerprints))
                    if observed := prior_observations.get(lookup):
                        events.append(IncrementalEvent(
                            "CREATIVE_REOBSERVED", run_id, group.group_id, now, observed, current,
                            ["mesmos IDs, pagina e destino reobservados; a oferta nao foi conciliada automaticamente"],
                        ))
                        previous = observed
                events.extend(self._derive_events(run_id, now, group.group_id, previous, current))
            for event in events:
                db.execute("""
                    INSERT OR IGNORE INTO incremental_events
                    (run_id,group_id,event_type,occurred_at,before_json,after_json,reasons_json)
                    VALUES(?,?,?,?,?,?,?)
                """, (event.run_id, event.group_id, event.event_type, event.occurred_at,
                      json.dumps(event.before, ensure_ascii=False, sort_keys=True) if event.before else None,
                      json.dumps(event.after, ensure_ascii=False, sort_keys=True),
                      json.dumps(event.reasons, ensure_ascii=False)))
            for group in report.groups:
                group_events = [event for event in events if event.group_id == group.group_id]
                item = self._queue_item(run_id, now, group.__dict__, group_events)
                if item:
                    db.execute("""
                        INSERT OR IGNORE INTO review_queue
                        (run_id,group_id,queue_type,priority,created_at,reason,event_types_json,group_json)
                        VALUES(?,?,?,?,?,?,?,?)
                    """, (item.run_id, item.group_id, item.queue_type, item.priority, item.created_at,
                          item.reason, json.dumps(item.event_types),
                          json.dumps(item.group, ensure_ascii=False, sort_keys=True)))
        return events

    def read_lp_cache(self, url: str, earliest: float, version: int) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT result_json FROM lp_fetch_cache
                   WHERE requested_url=? AND identity_version=? AND fetched_epoch>?""",
                (url, version, earliest),
            ).fetchone()
        return json.loads(row["result_json"]) if row else None

    def write_lp_cache(self, url: str, result: dict, version: int, *, record_history: bool = True) -> None:
        fetched_epoch = datetime.fromisoformat(result["fetched_at"]).timestamp()
        values = (url, version, fetched_epoch, json.dumps(result, ensure_ascii=False))
        with self.connect() as db:
            db.execute(
                """INSERT INTO lp_fetch_cache(requested_url, identity_version, fetched_epoch, result_json)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(requested_url, identity_version) DO UPDATE SET
                     fetched_epoch=excluded.fetched_epoch, result_json=excluded.result_json
                   WHERE excluded.fetched_epoch >= lp_fetch_cache.fetched_epoch""",
                values,
            )

            if record_history:
                db.execute(
                    """INSERT OR IGNORE INTO lp_fetch_history
                       (requested_url, identity_version, fetched_epoch, result_json) VALUES (?, ?, ?, ?)""",
                    values,
                )

    def run_group_snapshots(self, run_id: int) -> list[dict]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT evidence_json FROM group_history WHERE run_id=? ORDER BY group_id", (run_id,)
            ).fetchall()
        return [json.loads(row["evidence_json"]) for row in rows]

    def prior_full_group_counts(self, cycle: CollectionCycle) -> dict[str, int]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT group_id,related_ads_count FROM group_history WHERE run_id=(
                    SELECT MAX(r.run_id) FROM cycle_runs r WHERE r.cycle_id=(
                        SELECT c.cycle_id FROM collection_cycles c
                        WHERE c.started_at<? AND c.status='COMPLETED' AND c.official_plan=1
                          AND c.plan_hash=? ORDER BY c.started_at DESC, c.rowid DESC LIMIT 1
                    )
                )
            """, (cycle.started_at, cycle.plan_hash)).fetchall()
        return dict(rows)

    def latest_group_evidence(self, group_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT evidence_json FROM group_history WHERE group_id=? ORDER BY run_id DESC LIMIT 1",
                (group_id,),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def known_group_ids(self) -> set[str]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT COALESCE(m.canonical_id, g.group_id) FROM groups g
                LEFT JOIN media_group_aliases m ON m.legacy_id=g.group_id
            """).fetchall()
        return {str(row[0]) for row in rows}

    def reconcile_cycle(
        self, cycle_id: str, run_id: int, observed_group_ids: set[str], *, full: bool,
        connection: sqlite3.Connection | None = None,
    ) -> list[IncrementalEvent]:
        """Update freshness. Absence is actionable only after two consecutive FULL cycles."""
        now = datetime.now(timezone.utc).isoformat()
        emitted: list[IncrementalEvent] = []
        with (nullcontext(connection) if connection is not None else self.connect()) as db:
            # V1 identities cannot be treated as missing V2 offers.
            # A media alias target becomes eligible for absence tracking once observed and registered.
            known = {str(row[0]) for row in db.execute("""
                SELECT DISTINCT g.group_id FROM group_history h
                LEFT JOIN media_group_aliases m ON m.legacy_id=h.group_id
                JOIN groups g ON g.group_id=COALESCE(m.canonical_id, h.group_id)
                WHERE json_extract(h.evidence_json, '$.grouping_version')=2
            """)}
            for group_id in observed_group_ids:
                db.execute("""INSERT INTO group_freshness
                    (group_id,last_seen_cycle,last_seen_at,consecutive_full_absences,freshness_status)
                    VALUES(?,?,?,0,'ACTIVE_OBSERVED') ON CONFLICT(group_id) DO UPDATE SET
                    last_seen_cycle=excluded.last_seen_cycle,last_seen_at=excluded.last_seen_at,
                    consecutive_full_absences=0,freshness_status='ACTIVE_OBSERVED'""", (group_id, cycle_id, now))
            if not full:
                return emitted
            for group_id in known - observed_group_ids:
                row = db.execute(
                    "SELECT consecutive_full_absences FROM group_freshness WHERE group_id=?", (group_id,)
                ).fetchone()
                count = (int(row[0]) if row else 0) + 1
                freshness = "POSSIBLY_GONE" if count >= 2 else "STALE"
                db.execute("""INSERT INTO group_freshness
                    (group_id,consecutive_full_absences,freshness_status) VALUES(?,?,?)
                    ON CONFLICT(group_id) DO UPDATE SET consecutive_full_absences=?,freshness_status=?""",
                    (group_id, count, freshness, count, freshness))
                if count != 2:
                    continue
                before_row = db.execute("""SELECT evidence_json FROM group_history
                    WHERE group_id=? ORDER BY run_id DESC LIMIT 1""", (group_id,)).fetchone()
                before = json.loads(before_row[0]) if before_row else {}
                if before.get("final_status") != "CANDIDATO_RADAR":
                    continue
                after = dict(before)
                after.update({
                    "freshness_status": freshness,
                    "consecutive_full_absences": count,
                    "coverage_context": {"kind": "FULL_COVERAGE", "cycle_id": cycle_id},
                })
                event = IncrementalEvent(
                    "LEFT_RADAR", run_id, group_id, now, before, after,
                    ["ausente em 2 ciclos FULL consecutivos; sinal conservador, nao prova que a oferta morreu"],
                )
                db.execute("""INSERT OR IGNORE INTO incremental_events
                    (run_id,group_id,event_type,occurred_at,before_json,after_json,reasons_json)
                    VALUES(?,?,?,?,?,?,?)""", (
                    run_id, group_id, event.event_type, now,
                    json.dumps(before, ensure_ascii=False, sort_keys=True),
                    json.dumps(after, ensure_ascii=False, sort_keys=True), json.dumps(event.reasons),
                ))
                snapshot = dict(after)
                snapshot["previous"] = before
                db.execute("""INSERT OR IGNORE INTO review_queue
                    (run_id,group_id,queue_type,priority,created_at,reason,event_types_json,group_json)
                    VALUES(?,?, 'SAIU_DO_RADAR',50,?,?,?,?)""", (
                    run_id, group_id, now,
                    "duas ausencias FULL; requer confirmacao humana e nao afirma morte da oferta",
                    json.dumps(["LEFT_RADAR"]), json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                ))
                emitted.append(event)
        return emitted

    def freshness(self, group_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM group_freshness WHERE group_id=?", (group_id,)).fetchone()
        return dict(row) if row else None

    @staticmethod
    def _derive_events(run_id: int, now: str, group_id: str, before: dict | None,
                       after: dict) -> list[IncrementalEvent]:
        def event(kind: str, reasons: list[str]) -> IncrementalEvent:
            return IncrementalEvent(kind, run_id, group_id, now, before, after, reasons)

        if before is None:
            return [event("NEW_GROUP", ["grupo estavel observado pela primeira vez"])]
        result: list[IncrementalEvent] = []
        old_ids, new_ids = set(before.get("ad_ids", [])), set(after.get("ad_ids", []))
        added = sorted(new_ids - old_ids)
        if added:
            result.append(event("NEW_ADS", [f"novos IDs: {', '.join(added)}"]))
        old_count = int(before.get("related_ads_count", 0))
        new_count = int(after.get("related_ads_count", 0))
        if new_count > old_count:
            result.append(event("COUNT_INCREASED", [f"contagem aumentou de {old_count} para {new_count}"]))
        old_status, new_status = before.get("final_status"), after.get("final_status")
        if old_status != new_status:
            result.append(event("STATUS_CHANGED", [f"status tecnico mudou de {old_status} para {new_status}"]))
        if old_status != "CANDIDATO_RADAR" and new_status == "CANDIDATO_RADAR":
            result.append(event("ENTERED_RADAR", ["grupo passou a atender somente aos filtros tecnicos do Radar"]))
        coverage = after.get("coverage_context") or {}
        if (
            old_status == "CANDIDATO_RADAR" and new_status != "CANDIDATO_RADAR" and old_ids <= new_ids
            and coverage.get("kind") != "PARTIAL_COVERAGE"
        ):
            result.append(event("LEFT_RADAR", ["grupo deixou de atender aos filtros tecnicos do Radar"]))
        evidence_fields = (
            "keywords", "ad_ids", "start_dates", "destination_urls",
            "destination_domains", "grouping_confidence", "criterion_5_plus", "criterion_max_age",
            "confidence_reasons", "conflicting_evidence", "noise_flags", "grouping_migration", "separation_level",
        )
        changed = [field for field in evidence_fields if before.get(field) != after.get(field)]
        if changed:
            result.append(event("EVIDENCE_CHANGED", [f"evidencias alteradas: {', '.join(changed)}"]))
        return result

    @staticmethod
    def _queue_item(run_id: int, now: str, group: dict,
                    events: list[IncrementalEvent]) -> ReviewQueueItem | None:
        kinds = {event.event_type for event in events}
        status = group["final_status"]
        review = group["review_status"]
        if group.get("grouping_migration") == "AMBIGUOUS" and kinds & {"NEW_GROUP", "EVIDENCE_CHANGED"}:
            queue_type, priority, reason = "REVISAR_AMBIGUIDADE", 40, "historico V1 exige conciliacao humana"
        elif "ENTERED_RADAR" in kinds:
            queue_type, priority, reason = "ENTROU_NO_RADAR", 10, "entrou nos filtros tecnicos do Radar"
        elif "NEW_GROUP" in kinds and status == "CANDIDATO_RADAR":
            queue_type, priority, reason = "NOVO_CANDIDATO", 20, "novo grupo ja atende aos filtros tecnicos"
        elif status == "CANDIDATO_RADAR" and kinds & {"NEW_ADS", "COUNT_INCREASED"}:
            queue_type, priority, reason = "NOVOS_ANUNCIOS", 30, "candidato conhecido recebeu novas evidencias"
        elif status == "INVESTIGAR" and kinds & {"NEW_GROUP", "EVIDENCE_CHANGED", "STATUS_CHANGED"}:
            queue_type, priority, reason = "REVISAR_AMBIGUIDADE", 40, "agrupamento ambiguo exige revisao humana"
        elif "LEFT_RADAR" in kinds:
            queue_type, priority, reason = (
                "SAIU_DO_RADAR", 50, "deixou os filtros tecnicos; decisao humana foi preservada"
            )
        elif kinds == {"EVIDENCE_CHANGED"} and group.get("grouping_migration") == "MATCHED" and (
            events[0].before or {}
        ).get("grouping_version") is None:
            return None
        elif kinds & {"NEW_GROUP", "NEW_ADS", "COUNT_INCREASED", "STATUS_CHANGED", "EVIDENCE_CHANGED"}:
            queue_type, priority, reason = "ACOMPANHAR", 60, "mudanca material observada"
        else:
            return None
        snapshot = dict(group)
        snapshot["previous"] = events[0].before if events else None
        return ReviewQueueItem(run_id, group["group_id"], queue_type, priority, now, review, reason,
                               sorted(kinds), snapshot)

    def events(self, run_id: int | None = None) -> list[IncrementalEvent]:
        where, params = ("WHERE run_id=?", (run_id,)) if run_id is not None else ("", ())
        with self.connect() as db:
            rows = db.execute(
                f"SELECT * FROM incremental_events {where} ORDER BY run_id,group_id,event_type", params
            ).fetchall()
        return [IncrementalEvent(row["event_type"], row["run_id"], row["group_id"], row["occurred_at"],
                                 json.loads(row["before_json"]) if row["before_json"] else None,
                                 json.loads(row["after_json"]), json.loads(row["reasons_json"])) for row in rows]

    def events_since(self, run_id: int) -> list[IncrementalEvent]:
        return [event for event in self.events() if event.run_id > run_id]

    def review_queue(self, run_id: int | None = None, *, include_acknowledged: bool = False) -> list[ReviewQueueItem]:
        clauses, params = [], []
        if run_id is not None:
            clauses.append("q.run_id=?")
            params.append(run_id)
        if not include_acknowledged:
            clauses.append("q.acknowledged_at IS NULL")
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            rows = db.execute(f"""SELECT q.*,g.review_status FROM review_queue q JOIN groups g USING(group_id)
                {where} ORDER BY q.priority,q.run_id,q.group_id""", params).fetchall()
        return [ReviewQueueItem(row["run_id"], row["group_id"], row["queue_type"], row["priority"],
                                row["created_at"], row["review_status"], row["reason"],
                                json.loads(row["event_types_json"]), json.loads(row["group_json"]),
                                row["acknowledged_at"]) for row in rows]

    def review_queue_since(self, run_id: int) -> list[ReviewQueueItem]:
        return [item for item in self.review_queue() if item.run_id > run_id]

    def acknowledge(self, run_id: int, group_id: str) -> None:
        with self.connect() as db:
            if db.execute("UPDATE review_queue SET acknowledged_at=? WHERE run_id=? AND group_id=?",
                          (datetime.now(timezone.utc).isoformat(), run_id, group_id)).rowcount != 1:
                raise KeyError((run_id, group_id))

    def review_statuses(self) -> dict[str, str]:
        with self.connect() as db:
            return dict(db.execute("SELECT group_id,review_status FROM groups"))

    def set_review_status(self, group_id: str, status: str) -> None:
        if status not in REVIEW_STATUSES:
            raise ValueError(f"invalid review status: {status}")
        with self.connect() as db:
            if db.execute("""
                UPDATE groups SET review_status=? WHERE group_id=?
                    OR group_id=(SELECT group_id FROM grouping_aliases WHERE legacy_group_id=?)
                    OR group_id=(SELECT legacy_group_id FROM grouping_aliases WHERE group_id=?)
            """, (status, group_id, group_id, group_id)).rowcount == 0:
                raise KeyError(group_id)

    def group_history(self, group_id: str) -> list[int]:
        with self.connect() as db:
            rows = db.execute(
                """SELECT related_ads_count FROM group_history WHERE group_id=?
                   OR group_id=(SELECT legacy_group_id FROM grouping_aliases WHERE group_id=?)
                   OR group_id IN (SELECT legacy_id FROM media_group_aliases WHERE canonical_id=?)
                   ORDER BY run_id, group_id""", (group_id, group_id, group_id)
            ).fetchall()
        return [int(row[0]) for row in rows]
