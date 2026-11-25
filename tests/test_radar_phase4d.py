from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
from dataclasses import asdict

import pytest

from meta_ads_collector.events import EventEmitter
from meta_ads_collector.exceptions import RateLimitError
from radar.batch import run_batch
from radar.cli import _print_report, main
from radar.cycle import get_cycle, run_cycle, start_cycle
from radar.operations import IncompleteSearch, collect_keyword, collection_lock, write_reports
from radar.storage import RadarStore
from tests.test_radar_phase4a import CONFIG, ad, entries


def test_process_exit_frees_lock(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    command = (
        "import os,sys; from pathlib import Path; from radar.operations import collection_lock; "
        "lock=collection_lock(Path(sys.argv[1])); lock.__enter__(); os._exit(17)"
    )
    with (
        collection_lock(store.path),
        pytest.raises(RuntimeError, match="outra execução"),
        collection_lock(store.path),
    ):
        pytest.fail("overlapping collection")
    result = subprocess.run([sys.executable, "-c", command, str(store.path)], check=False, timeout=15)  # noqa: S603
    assert result.returncode == 17
    with collection_lock(store.path):
        pass


def test_resume_after_interruption_keeps_only_committed_keywords(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    plan = entries()
    cycle = start_cycle(plan, store, CONFIG)
    calls = []

    def interrupted(entry, _):
        calls.append(entry.id)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return []

    with pytest.raises(KeyboardInterrupt):
        run_cycle(store, cycle.cycle_id, plan, CONFIG, search=interrupted)
    before = get_cycle(store, cycle.cycle_id)
    assert before.succeeded == 1
    assert before.coverage_kind == "PARTIAL_COVERAGE"
    result, _ = run_cycle(
        store,
        cycle.cycle_id,
        plan,
        CONFIG,
        search=lambda entry, _: calls.append(entry.id) or [],
    )
    assert result.coverage_kind == "FULL_COVERAGE"
    assert calls.count(plan[0].id) == 1 and calls.count(plan[1].id) == 2
    assert store.run_row(result.run_ids[0])["status"] == "INTERRUPTED"


def test_atomic_keyword_saved_with_origins_and_rollback(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    plan = entries(1)
    cycle = start_cycle(plan, store, CONFIG)
    with store.connect() as db:
        db.execute("""CREATE TRIGGER fail_origin BEFORE INSERT ON ad_observations
                       BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END""")
    with pytest.raises(sqlite3.IntegrityError):
        run_cycle(store, cycle.cycle_id, plan, CONFIG, search=lambda *_: [ad(1)])
    assert store.cycle_keywords(cycle.cycle_id)[0]["status"] == "PENDING"
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM keyword_results").fetchone()[0] == 0
        db.execute("DROP TRIGGER fail_origin")
    resumed, report = run_cycle(store, cycle.cycle_id, plan, CONFIG, search=lambda *_: [ad(1)])
    assert resumed.succeeded == report.collection["observations"] == 1


def test_finalization_rollback_preserves_human_review_and_no_duplicate_absence(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    plan = entries()
    initial = start_cycle(plan, store, CONFIG)
    _, report = run_cycle(store, initial.cycle_id, plan, CONFIG, search=lambda *_: [ad(i) for i in range(5)])
    group_id = report.groups[0].group_id
    store.set_review_status(group_id, "APROVADO")
    next_cycle = start_cycle(plan, store, CONFIG)
    original = store.reconcile_cycle

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt

    store.reconcile_cycle = interrupted
    with pytest.raises(KeyboardInterrupt):
        run_cycle(store, next_cycle.cycle_id, plan, CONFIG, search=lambda *_: [])
    assert store.freshness(group_id)["consecutive_full_absences"] == 0
    assert get_cycle(store, next_cycle.cycle_id).coverage_kind == "PARTIAL_COVERAGE"
    store.reconcile_cycle = original

    def no_network(*args):
        pytest.fail("all keyword checkpoints were already saved")

    resumed, _ = run_cycle(store, next_cycle.cycle_id, plan, CONFIG, search=no_network)
    assert resumed.coverage_kind == "FULL_COVERAGE"
    assert store.freshness(group_id)["consecutive_full_absences"] == 1
    assert not [event for event in store.events() if event.event_type == "LEFT_RADAR"]
    with store.connect() as db:
        assert db.execute("SELECT review_status FROM groups WHERE group_id=?", (group_id,)).fetchone()[0] == "APROVADO"


def test_rate_limit_preserves_pending_and_diagnostics(tmp_path, caplog):
    store = RadarStore(tmp_path / "radar.sqlite3")
    plan = entries()
    cycle = start_cycle(plan, store, CONFIG)
    attempts = []

    def search(entry, _):
        attempts.append(entry.id)
        if len(attempts) == 1:
            raise TimeoutError
        if len(attempts) == 3:
            raise RateLimitError("secret URL should not be logged")
        return []

    with caplog.at_level("INFO", logger="radar.operations"):
        result, _ = run_cycle(store, cycle.cycle_id, plan, CONFIG, search=search)
    assert attempts == [plan[0].id, plan[0].id, plan[1].id]
    assert (result.succeeded, result.failed, result.pending, result.reason) == (1, 1, 104, "rate_limit")
    assert "secret" not in caplog.text
    assert {json.loads(record.message)["event"] for record in caplog.records} >= {"cycle_paused", "keyword_failure"}


def test_collector_parse_failure_is_not_reported_as_empty():
    class Collector:
        def __init__(self):
            self.event_emitter = EventEmitter()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def search(self, **kwargs):
            self.event_emitter.emit("error_occurred", {"context": "Failed to parse ad"})
            self.event_emitter.emit("collection_finished", {"total_pages": 1})
            return []

    with pytest.raises(IncompleteSearch):
        collect_keyword(entries(1)[0], CONFIG, collector_factory=Collector)


def test_corrupt_checkpoint_cannot_finalize(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    plan = entries()
    cycle = start_cycle(plan, store, CONFIG)
    run_cycle(store, cycle.cycle_id, plan, CONFIG, search=lambda *_: [], max_keywords=1)
    with store.connect() as db:
        db.execute("UPDATE cycle_keywords SET raw_count=1 WHERE status='OK'")
    with pytest.raises(ValueError, match="inconsistent checkpoint"):
        run_cycle(store, cycle.cycle_id, plan, CONFIG, search=lambda *_: pytest.fail("corrupt checkpoint"))
    assert get_cycle(store, cycle.cycle_id).coverage_kind == "PARTIAL_COVERAGE"


def test_atomic_report_and_utf8_console(tmp_path, monkeypatch):
    output = tmp_path / "report.json"
    output.write_text("old", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        write_reports([(output, "new"), (tmp_path / "missing" / "output", "new")])
    assert output.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.iterdir()) == [output]

    store = RadarStore(tmp_path / "radar.sqlite3")
    plan = entries(1)
    started = start_cycle(plan, store, CONFIG)
    run_cycle(store, started.cycle_id, plan, CONFIG, search=lambda *_: [ad(1)])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "meta-ads-radar",
            "--cycle-action",
            "report",
            "--cycle-id",
            started.cycle_id,
            "--database",
            str(store.path),
            "--json-output",
            str(output),
        ],
    )
    buffer = io.BytesIO()
    with io.TextIOWrapper(buffer, encoding="cp1252") as console:
        _print_report("Ofertá 中文", stream=console)
        console.flush()
        assert b"\\u4e2d" in buffer.getvalue()
    assert main() == 0
    assert json.loads(output.read_text(encoding="utf-8"))["collection"]["observations"] == 1


def test_batch_resumes_from_payload_before_report_exists(tmp_path):
    store = RadarStore(tmp_path / "radar.sqlite3")
    plan = entries(2)
    run_id = store.start_run(asdict(CONFIG))

    def interrupted(entry, _):
        if entry.id == plan[1].id:
            raise KeyboardInterrupt
        return [ad(1)]

    with pytest.raises(KeyboardInterrupt):
        run_batch(plan, store, CONFIG, run_id=run_id, search=interrupted)
    calls = []
    _, report, _ = run_batch(plan, store, CONFIG, run_id=run_id, search=lambda e, _: calls.append(e.id) or [ad(2)])
    assert calls == [plan[1].id]
    assert report.collection["observations"] == 2
