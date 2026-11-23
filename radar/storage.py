"""SQLite persistence for runs, observations, group history, and human review."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .models import RadarReport

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

    def persist_report(self, run_id: int, report: RadarReport) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as db:
            for group in report.groups:
                db.execute("""
                    INSERT INTO groups(group_id,review_status,first_seen,last_seen) VALUES(?, 'NOVO', ?, ?)
                    ON CONFLICT(group_id) DO UPDATE SET last_seen=excluded.last_seen
                """, (group.group_id, now, now))
                review = db.execute(
                    "SELECT review_status FROM groups WHERE group_id=?", (group.group_id,)
                ).fetchone()["review_status"]
                group.review_status = review
                db.execute("""
                    INSERT OR REPLACE INTO group_history
                    (run_id,group_id,observed_at,related_ads_count,final_status,evidence_json)
                    VALUES(?,?,?,?,?,?)
                """, (run_id, group.group_id, now, group.related_ads_count, group.final_status,
                      json.dumps(group.__dict__, ensure_ascii=False, sort_keys=True)))

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
