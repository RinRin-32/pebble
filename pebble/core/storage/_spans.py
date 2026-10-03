"""``workstream_spans`` reads and writes, shared by both storage backends.

The SQL is plain SQLAlchemy Core with nothing dialect-specific, so the
SQLite and PostgreSQL backends call these with their own connection rather
than carrying two copies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa

from pebble.core.storage._schema import workstream_spans

SPAN_KINDS = frozenset({"turn", "llm", "tool"})

_INT_FIELDS = ("step", "tok_in", "tok_out", "tok_cache_read", "tok_cache_write")
_TEXT_FIELDS = ("turn_id", "call_id", "parent_call_id", "name", "status")


def span_rows(ws_id: str, spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Normalise caller dicts into insertable rows; drops malformed spans."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    rows: list[dict[str, Any]] = []
    for sp in spans:
        kind = sp.get("kind")
        started = sp.get("started_at")
        ended = sp.get("ended_at")
        if kind not in SPAN_KINDS or not isinstance(started, int) or not isinstance(ended, int):
            continue
        row: dict[str, Any] = {
            "ws_id": ws_id,
            "kind": kind,
            "started_at": started,
            "ended_at": max(ended, started),
            "ttft_ms": sp.get("ttft_ms") if isinstance(sp.get("ttft_ms"), int) else None,
            "created": now,
        }
        for f in _INT_FIELDS:
            v = sp.get(f)
            row[f] = v if isinstance(v, int) else 0
        for f in _TEXT_FIELDS:
            v = sp.get(f)
            row[f] = v if isinstance(v, str) else ""
        rows.append(row)
    return rows


def save_spans(conn: Any, ws_id: str, spans: list[dict[str, Any]]) -> int:
    rows = span_rows(ws_id, spans)
    if rows:
        conn.execute(sa.insert(workstream_spans), rows)
    return len(rows)


def list_spans(conn: Any, ws_id: str, since: int = 0, limit: int = 5000) -> list[dict[str, Any]]:
    c = workstream_spans.c
    q = (
        sa.select(
            c.turn_id,
            c.step,
            c.kind,
            c.call_id,
            c.parent_call_id,
            c.name,
            c.started_at,
            c.ended_at,
            c.ttft_ms,
            c.status,
            c.tok_in,
            c.tok_out,
            c.tok_cache_read,
            c.tok_cache_write,
        )
        .where(c.ws_id == ws_id)
        .where(c.started_at >= since)
        .order_by(c.started_at, c.id)
        .limit(max(1, min(limit, 20000)))
    )
    return [dict(r._mapping) for r in conn.execute(q)]


def delete_spans(conn: Any, ws_id: str) -> None:
    conn.execute(sa.delete(workstream_spans).where(workstream_spans.c.ws_id == ws_id))
