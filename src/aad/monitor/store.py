"""Event log for monitored outputs.

Every verification is recorded, not just the failures. A dashboard that only shows
blocks cannot tell you whether the system is getting safer or merely getting quieter,
and the abstention rate is only meaningful against the grounding rate it trades off.

SQLite, one file, no server — the same offline-first constraint as the rest of the
system. A shop with no connectivity still gets its audit trail, which is also what
makes this usable as TISAX evidence: every answer a technician acted on has a row.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aad.monitor.pipeline import MonitorResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS monitor_events (
    id                 TEXT PRIMARY KEY,
    created_at         REAL NOT NULL,
    task_type          TEXT NOT NULL,
    verdict            TEXT NOT NULL,
    abstention_reason  TEXT,
    needs_human_review INTEGER NOT NULL DEFAULT 0,
    faithfulness       REAL NOT NULL,
    citation_accuracy  REAL NOT NULL,
    mean_semantic      REAL NOT NULL,
    claim_count        INTEGER NOT NULL DEFAULT 0,
    fabrication_count  INTEGER NOT NULL DEFAULT 0,
    latency_ms         REAL NOT NULL DEFAULT 0,
    vehicle            TEXT,
    question           TEXT,
    output             TEXT,
    payload            TEXT NOT NULL,
    reviewed_at        REAL,
    reviewer           TEXT,
    review_outcome     TEXT,
    review_note        TEXT
);
CREATE INDEX IF NOT EXISTS monitor_created_idx ON monitor_events (created_at);
CREATE INDEX IF NOT EXISTS monitor_task_idx    ON monitor_events (task_type);
CREATE INDEX IF NOT EXISTS monitor_verdict_idx ON monitor_events (verdict);
CREATE INDEX IF NOT EXISTS monitor_review_idx  ON monitor_events (needs_human_review, reviewed_at);
"""

# Verdicts that mean "a human has to look at this before a wrench moves".
REVIEW_VERDICTS = ("blocked", "abstained", "escalated")
REVIEW_OUTCOMES = ("confirmed", "corrected", "rejected")


class MonitorStore:
    """Append-only log of monitor results, plus the aggregates the dashboard reads."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # --- writing ---------------------------------------------------------
    def record(
        self,
        result: MonitorResult,
        *,
        question: str = "",
        output: str = "",
        vehicle: str | None = None,
        event_id: str | None = None,
    ) -> str:
        """Persist one verification. Returns the event id.

        The full result is stored as JSON alongside the flattened columns: the columns
        make the dashboard queries cheap, the JSON keeps the per-claim evidence a
        reviewer needs to actually adjudicate the call.
        """
        payload = result.to_dict()
        event_id = event_id or uuid.uuid4().hex
        self._conn.execute(
            "INSERT INTO monitor_events ("
            "id, created_at, task_type, verdict, abstention_reason, needs_human_review, "
            "faithfulness, citation_accuracy, mean_semantic, claim_count, fabrication_count, "
            "latency_ms, vehicle, question, output, payload"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                event_id,
                time.time(),
                result.task_type,
                result.verdict,
                result.abstention_reason,
                int(result.needs_human_review),
                result.faithfulness,
                result.citation_accuracy,
                result.mean_semantic,
                len(result.claims),
                len(result.fabrications),
                result.latency_ms,
                vehicle,
                question[:2000],
                output[:8000],
                json.dumps(payload, default=str),
            ),
        )
        self._conn.commit()
        return event_id

    def resolve(
        self, event_id: str, *, reviewer: str, outcome: str, note: str = ""
    ) -> bool:
        """Close out a human review. Returns False if the id is unknown.

        Resolution never rewrites the verdict — the original call stands in the record.
        A corrected answer is a new fact about the review, not an edit to history.
        """
        if outcome not in REVIEW_OUTCOMES:
            raise ValueError(f"outcome must be one of {REVIEW_OUTCOMES}, got {outcome!r}")
        cursor = self._conn.execute(
            "UPDATE monitor_events SET reviewed_at = ?, reviewer = ?, review_outcome = ?, "
            "review_note = ? WHERE id = ?",
            (time.time(), reviewer, outcome, note[:2000], event_id),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    # --- reading ---------------------------------------------------------
    def get(self, event_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM monitor_events WHERE id = ?", (event_id,)
        ).fetchone()
        return _row_to_event(row) if row else None

    def recent(
        self,
        *,
        limit: int = 50,
        task_type: str | None = None,
        verdict: str | None = None,
        needs_review: bool | None = None,
        since: float | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if task_type:
            clauses.append("task_type = ?")
            params.append(task_type)
        if verdict:
            clauses.append("verdict = ?")
            params.append(verdict)
        if needs_review is not None:
            clauses.append("needs_human_review = ?")
            params.append(int(needs_review))
        if since is not None:
            clauses.append("created_at >= ?")
            params.append(since)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM monitor_events{where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def review_queue(self, limit: int = 50) -> list[dict[str, Any]]:
        """Outputs waiting on a human, oldest first — a queue, not a feed."""
        rows = self._conn.execute(
            "SELECT * FROM monitor_events WHERE needs_human_review = 1 AND reviewed_at IS NULL "
            "ORDER BY created_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_event(r) for r in rows]

    def summary(self, *, since: float | None = None) -> dict[str, Any]:
        """Headline numbers for the dashboard.

        `hallucination_rate` is the one that matters, and it is deliberately computed
        over events that made a claim at all: an output with nothing to verify is not
        evidence of grounding, and letting it into the denominator would dilute the
        rate toward zero exactly when the system starts answering less.
        """
        where, params = ("WHERE created_at >= ?", [since]) if since is not None else ("", [])
        row = self._conn.execute(
            f"""
            SELECT COUNT(*)                                        AS events,
                   SUM(CASE WHEN verdict != 'no_claims' THEN 1 ELSE 0 END) AS claim_events,
                   SUM(CASE WHEN verdict = 'blocked' THEN 1 ELSE 0 END)    AS blocked,
                   SUM(CASE WHEN verdict = 'grounded' THEN 1 ELSE 0 END)   AS grounded,
                   SUM(CASE WHEN verdict = 'abstained' THEN 1 ELSE 0 END)  AS abstained,
                   SUM(CASE WHEN verdict = 'escalated' THEN 1 ELSE 0 END)  AS escalated,
                   SUM(CASE WHEN verdict = 'no_claims' THEN 1 ELSE 0 END)  AS no_claims,
                   SUM(needs_human_review)                         AS flagged,
                   SUM(CASE WHEN needs_human_review = 1 AND reviewed_at IS NULL THEN 1 ELSE 0 END)
                                                                   AS pending_review,
                   SUM(claim_count)                                AS claims,
                   SUM(fabrication_count)                          AS fabrications,
                   AVG(faithfulness)                               AS faithfulness,
                   AVG(citation_accuracy)                          AS citation_accuracy,
                   AVG(mean_semantic)                              AS mean_semantic,
                   AVG(latency_ms)                                 AS latency_ms
            FROM monitor_events {where}
            """,
            params,
        ).fetchone()

        events = row["events"] or 0
        claim_events = row["claim_events"] or 0
        claims = row["claims"] or 0
        fabrications = row["fabrications"] or 0
        return {
            "events": events,
            "claim_events": claim_events,
            "verdicts": {
                "grounded": row["grounded"] or 0,
                "abstained": row["abstained"] or 0,
                "escalated": row["escalated"] or 0,
                "blocked": row["blocked"] or 0,
                "no_claims": row["no_claims"] or 0,
            },
            "claims": claims,
            "fabrications": fabrications,
            "flagged_for_review": row["flagged"] or 0,
            "pending_review": row["pending_review"] or 0,
            "hallucination_rate": _ratio(row["blocked"] or 0, claim_events),
            "fabricated_claim_rate": _ratio(fabrications, claims),
            "abstention_rate": _ratio(
                (row["abstained"] or 0) + (row["escalated"] or 0), claim_events
            ),
            "grounding_rate": _ratio(row["grounded"] or 0, claim_events),
            "mean_faithfulness": _round(row["faithfulness"]),
            "mean_citation_accuracy": _round(row["citation_accuracy"]),
            "mean_semantic_consistency": _round(row["mean_semantic"]),
            "mean_latency_ms": _round(row["latency_ms"], 2),
        }

    def by_task_type(self, *, since: float | None = None) -> list[dict[str, Any]]:
        """Per-task-type breakdown: where abstention concentrates is where coverage is thin."""
        where, params = ("WHERE created_at >= ?", [since]) if since is not None else ("", [])
        rows = self._conn.execute(
            f"""
            SELECT task_type,
                   COUNT(*)                                        AS events,
                   SUM(CASE WHEN verdict != 'no_claims' THEN 1 ELSE 0 END) AS claim_events,
                   SUM(CASE WHEN verdict = 'grounded' THEN 1 ELSE 0 END)   AS grounded,
                   SUM(CASE WHEN verdict = 'abstained' THEN 1 ELSE 0 END)  AS abstained,
                   SUM(CASE WHEN verdict = 'escalated' THEN 1 ELSE 0 END)  AS escalated,
                   SUM(CASE WHEN verdict = 'blocked' THEN 1 ELSE 0 END)    AS blocked,
                   SUM(CASE WHEN verdict = 'no_claims' THEN 1 ELSE 0 END)  AS no_claims,
                   SUM(fabrication_count)                          AS fabrications,
                   AVG(faithfulness)                               AS faithfulness,
                   AVG(citation_accuracy)                          AS citation_accuracy,
                   AVG(mean_semantic)                              AS mean_semantic
            FROM monitor_events {where}
            GROUP BY task_type
            ORDER BY events DESC
            """,
            params,
        ).fetchall()
        out = []
        for r in rows:
            claim_events = r["claim_events"] or 0
            out.append(
                {
                    "task_type": r["task_type"],
                    "events": r["events"],
                    "claim_events": claim_events,
                    "grounded": r["grounded"] or 0,
                    "abstained": r["abstained"] or 0,
                    "escalated": r["escalated"] or 0,
                    "blocked": r["blocked"] or 0,
                    "no_claims": r["no_claims"] or 0,
                    "fabrications": r["fabrications"] or 0,
                    "grounding_rate": _ratio(r["grounded"] or 0, claim_events),
                    "abstention_rate": _ratio(
                        (r["abstained"] or 0) + (r["escalated"] or 0), claim_events
                    ),
                    "hallucination_rate": _ratio(r["blocked"] or 0, claim_events),
                    "mean_faithfulness": _round(r["faithfulness"]),
                    "mean_citation_accuracy": _round(r["citation_accuracy"]),
                    "mean_semantic_consistency": _round(r["mean_semantic"]),
                }
            )
        return out

    def abstention_reasons(self, *, since: float | None = None) -> dict[str, int]:
        where, params = (
            ("AND created_at >= ?", [since]) if since is not None else ("", [])
        )
        rows = self._conn.execute(
            f"SELECT abstention_reason, COUNT(*) AS n FROM monitor_events "
            f"WHERE abstention_reason IS NOT NULL {where} GROUP BY abstention_reason "
            f"ORDER BY n DESC",
            params,
        ).fetchall()
        return {r["abstention_reason"]: r["n"] for r in rows}

    def timeseries(self, *, buckets: int = 14, bucket_seconds: float = 86400.0) -> list[dict]:
        """Recent history, oldest bucket first, with empty buckets kept.

        Dropping quiet days would make a gap look like a trend.
        """
        now = time.time()
        start = now - buckets * bucket_seconds
        rows = self._conn.execute(
            "SELECT created_at, verdict, faithfulness, citation_accuracy, fabrication_count "
            "FROM monitor_events WHERE created_at >= ?",
            (start,),
        ).fetchall()

        series: list[dict[str, Any]] = [
            {
                "bucket_start": start + i * bucket_seconds,
                "events": 0,
                "claim_events": 0,
                "grounded": 0,
                "abstained": 0,
                "escalated": 0,
                "blocked": 0,
                "no_claims": 0,
                "fabrications": 0,
                "_faith": 0.0,
                "_cite": 0.0,
            }
            for i in range(buckets)
        ]
        for r in rows:
            idx = min(int((r["created_at"] - start) // bucket_seconds), buckets - 1)
            bucket = series[idx]
            bucket["events"] += 1
            bucket[r["verdict"]] = bucket.get(r["verdict"], 0) + 1
            if r["verdict"] != "no_claims":
                bucket["claim_events"] += 1
            bucket["fabrications"] += r["fabrication_count"] or 0
            bucket["_faith"] += r["faithfulness"] or 0.0
            bucket["_cite"] += r["citation_accuracy"] or 0.0

        for bucket in series:
            n = bucket["events"]
            bucket["mean_faithfulness"] = round(bucket.pop("_faith") / n, 4) if n else None
            bucket["mean_citation_accuracy"] = round(bucket.pop("_cite") / n, 4) if n else None
            bucket["hallucination_rate"] = _ratio(bucket["blocked"], bucket["claim_events"])
        return series

    def fabrications(self, limit: int = 25) -> list[dict[str, Any]]:
        """The blocked outputs, with the offending claims pulled out.

        This is the list a maintainer reads: each entry is a value the model asserted
        that no cited source contained.
        """
        rows = self._conn.execute(
            "SELECT * FROM monitor_events WHERE fabrication_count > 0 "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            event = _row_to_event(r)
            claims = event.get("payload", {}).get("claims", [])
            out.append(
                {
                    "id": event["id"],
                    "created_at": event["created_at"],
                    "task_type": event["task_type"],
                    "vehicle": event["vehicle"],
                    "question": event["question"],
                    "claims": [c for c in claims if c.get("fabricated")],
                }
            )
        return out

    def purge(self, older_than_seconds: float) -> int:
        cutoff = time.time() - older_than_seconds
        cursor = self._conn.execute("DELETE FROM monitor_events WHERE created_at < ?", (cutoff,))
        self._conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._conn.close()


def _ratio(numerator: float, denominator: float) -> float | None:
    """None, not 0.0, when there is nothing to divide.

    A 0% hallucination rate over zero samples is not a safety claim, and rendering it
    as one on a dashboard is how people come to trust a system that has not been tested.
    """
    return round(numerator / denominator, 4) if denominator else None


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    event = dict(row)
    raw = event.pop("payload", "{}")
    try:
        event["payload"] = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        event["payload"] = {}
    event["needs_human_review"] = bool(event.get("needs_human_review"))
    return event


# Keyed by path, not a single global: a test settings object pointing at a tmp file
# must never end up writing into the shop's real audit log.
_stores: dict[Path, MonitorStore] = {}


def get_monitor_store(path: Path | None = None) -> MonitorStore:
    if path is None:
        from aad.config import get_settings

        path = get_settings().monitor_db
    resolved = Path(path).expanduser().resolve()
    store = _stores.get(resolved)
    if store is None:
        store = _stores[resolved] = MonitorStore(resolved)
    return store


def reset_monitor_store(paths: Iterable[Path] = ()) -> None:
    """Drop the cached stores. Tests use this to avoid cross-test bleed."""
    for store in _stores.values():
        store.close()
    _stores.clear()
    for path in paths:
        Path(path).unlink(missing_ok=True)
