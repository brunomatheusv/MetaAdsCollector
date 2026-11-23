"""SQLite persistence for runs, observations, group history, and human review."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import IncrementalEvent, RadarReport, ReviewQueueItem

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
            """)

    def start_run(self, config: dict) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            cur = db.execute(
                "INSERT INTO runs(started_at,status,config_json) VALUES(?, 'RUNNING', ?)",
                (now, json.dumps(config, ensure_ascii=False, sort_keys=True)),
            )
            return int(cur.lastrowid)

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

    def persist_report(self, run_id: int, report: RadarReport) -> list[IncrementalEvent]:
        now = datetime.now(timezone.utc).isoformat()
        events: list[IncrementalEvent] = []
        with self.connect() as db:
            for group in report.groups:
                current = dict(group.__dict__)
                previous_row = db.execute("""
                    SELECT gh.evidence_json FROM group_history gh
                    WHERE gh.group_id=? AND gh.run_id < ? ORDER BY gh.run_id DESC LIMIT 1
                """, (group.group_id, run_id)).fetchone()
                previous = json.loads(previous_row[0]) if previous_row else None
                db.execute("""
                    INSERT INTO groups(group_id,review_status,first_seen,last_seen) VALUES(?, 'NOVO', ?, ?)
                    ON CONFLICT(group_id) DO UPDATE SET last_seen=excluded.last_seen
                """, (group.group_id, now, now))
                review = db.execute(
                    "SELECT review_status FROM groups WHERE group_id=?", (group.group_id,)
                ).fetchone()["review_status"]
                group.review_status = review
                current["review_status"] = review
                db.execute("""
                    INSERT OR REPLACE INTO group_history
                    (run_id,group_id,observed_at,related_ads_count,final_status,evidence_json)
                    VALUES(?,?,?,?,?,?)
                """, (run_id, group.group_id, now, group.related_ads_count, group.final_status,
                      json.dumps(current, ensure_ascii=False, sort_keys=True)))
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
        if old_status == "CANDIDATO_RADAR" and new_status != "CANDIDATO_RADAR" and old_ids <= new_ids:
            result.append(event("LEFT_RADAR", ["grupo deixou de atender aos filtros tecnicos do Radar"]))
        evidence_fields = (
            "keywords", "ad_ids", "start_dates", "ages_days", "destination_urls",
            "destination_domains", "grouping_confidence", "criterion_5_plus", "criterion_max_age",
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
        if "ENTERED_RADAR" in kinds:
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

    def set_review_status(self, group_id: str, status: str) -> None:
        if status not in REVIEW_STATUSES:
            raise ValueError(f"invalid review status: {status}")
        with self.connect() as db:
            if db.execute("UPDATE groups SET review_status=? WHERE group_id=?", (status, group_id)).rowcount != 1:
                raise KeyError(group_id)

    def group_history(self, group_id: str) -> list[int]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT related_ads_count FROM group_history WHERE group_id=? ORDER BY run_id", (group_id,)
            ).fetchall()
        return [int(row[0]) for row in rows]
