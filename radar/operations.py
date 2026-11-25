"""Operational safeguards shared by the Radar collection entry points."""

from __future__ import annotations

import json
import logging
import os
import pickle
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

from meta_ads_collector import Ad, MetaAdsCollector
from meta_ads_collector.events import COLLECTION_FINISHED, ERROR_OCCURRED, RATE_LIMITED
from meta_ads_collector.exceptions import AuthenticationError, RateLimitError, SessionExpiredError

LOGGER = logging.getLogger("radar.operations")


def diagnostic(event: str, **fields) -> None:
    LOGGER.info(json.dumps({"event": event, **fields}, ensure_ascii=True, sort_keys=True))


@contextmanager
def collection_lock(database: Path):
    """A separate SQLite write lock survives contention and releases on process exit."""
    path = Path(str(database.resolve()) + ".lock.sqlite3")
    db = sqlite3.connect(path, timeout=0)
    try:
        try:
            db.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) in (5, 6) or "locked" in str(exc).lower():
                raise RuntimeError("Radar: outra execução usa este banco") from exc
            raise
        yield
    finally:
        db.close()


class IncompleteSearch(RuntimeError):
    """The collector returned observations but signaled an incomplete response."""


def collect_keyword(entry, config, *, collector_factory=MetaAdsCollector):
    complete = []
    errors = []
    with collector_factory() as collector:
        collector.event_emitter.on(COLLECTION_FINISHED, complete.append)
        collector.event_emitter.on(ERROR_OCCURRED, errors.append)
        collector.event_emitter.on(
            RATE_LIMITED,
            lambda event: diagnostic(
                "collector_rate_limit",
                keyword_id=entry.id,
                retry=event.data.get("retry_count"),
                wait_seconds=event.data.get("wait_seconds"),
            ),
        )
        ads = list(
            collector.search(
                query=entry.text,
                country=config.country,
                status=config.status.upper(),
                max_results=config.max_results_per_keyword,
            )
        )
    if errors or not complete or not complete[-1].data.get("total_pages"):
        raise IncompleteSearch("collector did not confirm a clean result")
    return ads


def failure_kind(exc: Exception) -> str:
    message = str(exc).casefold()
    if isinstance(exc, RateLimitError) or any(token in message for token in ("rate limit", "429")):
        return "rate_limit"
    if isinstance(exc, (AuthenticationError, SessionExpiredError)) or any(
        token in message for token in ("verification", "checkpoint", "blocked", "forbidden")
    ):
        return "access_blocked"
    if isinstance(exc, (TimeoutError, ConnectionError)) or any(token in message for token in ("timed out", "timeout")):
        return "transient_transport"
    return "incomplete_search" if isinstance(exc, IncompleteSearch) else "search_error"


def search_with_retry(search, entry, config, sleep, *, run_id: int):
    for attempt in (1, 2):
        try:
            return list(search(entry, config))
        except Exception as exc:
            kind = failure_kind(exc)
            diagnostic(
                "keyword_failure",
                run_id=run_id,
                keyword_id=entry.id,
                attempt=attempt,
                kind=kind,
                exception=type(exc).__name__,
            )
            if kind != "transient_transport" or attempt == 2:
                raise
            sleep(min(config.backoff_seconds, 30))
    raise AssertionError("bounded retry exhausted")


def write_reports(outputs: list[tuple[Path | None, str]]) -> None:
    """Stage UTF-8 files before replacing each destination atomically."""
    selected = [(path, content) for path, content in outputs if path is not None]
    paths = [path.resolve() for path, _ in selected]
    if len(set(paths)) != len(paths):
        raise ValueError("report output paths must be distinct")
    staged = []
    try:
        for (path, content), destination in zip(selected, paths):
            with tempfile.NamedTemporaryFile(
                dir=destination.parent,
                prefix=f".{path.name}.",
                delete=False,
                mode="w",
                encoding="utf-8",
                newline="\n",
            ) as file:
                staged.append((Path(file.name), destination))
                file.write(content)
                file.flush()
                os.fsync(file.fileno())
        for temporary, destination in staged:
            os.replace(temporary, destination)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def checkpoint_ads(row):
    """Validate trusted local payloads before they can provide positive or negative evidence."""
    try:
        ads = pickle.loads(row["payload"])  # noqa: S301 - private local checkpoint format
    except (TypeError, ValueError, EOFError, ImportError, AttributeError, pickle.UnpicklingError):
        raise ValueError(f"invalid checkpoint for keyword {row['keyword_id']}") from None
    if not isinstance(ads, list) or len(ads) != row["raw_count"] or any(not isinstance(ad, Ad) for ad in ads):
        raise ValueError(f"inconsistent checkpoint for keyword {row['keyword_id']}")
    return ads
