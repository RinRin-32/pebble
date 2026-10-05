"""Process-wide concurrency gates keyed by inference-server base URL.

Auxiliary LLM lanes (the intent judge today) share an inference server with
the main loop.  A local server such as llama.cpp with auto ``--parallel``
runs its slots over ONE KV pool, so an unbounded fan-out of side calls — a
batch of five tool calls spawns five judge evaluations at once — can exhaust
the pool and fail the main loop's in-flight stream ("Context size has been
exceeded.").  Holding a gate around each side call bounds that fan-out per
server without touching the main loop, which never takes a gate.

Gates are per process: each node bounds its own side calls.
"""

from __future__ import annotations

import contextlib
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

_POLL_SECONDS = 0.5

_lock = threading.Lock()
_gates: dict[tuple[str, int], threading.BoundedSemaphore] = {}


class GateAbandonedError(RuntimeError):
    """The caller's deadline abandoned the call while it queued for a gate."""


def _normalize(base_url: str) -> str:
    return base_url.strip().rstrip("/").lower()


def backend_gate(base_url: str, limit: int) -> threading.BoundedSemaphore | None:
    """Return the shared gate for *base_url*, or ``None`` when ungated.

    ``limit <= 0`` or an unknown URL means unlimited.  The limit is part of
    the key, so an operator retuning the setting gets a fresh gate instead of
    one sized for the old value.
    """
    url = _normalize(base_url)
    if limit <= 0 or not url:
        return None
    key = (url, limit)
    with _lock:
        gate = _gates.get(key)
        if gate is None:
            gate = _gates[key] = threading.BoundedSemaphore(limit)
        return gate


def client_base_url(client: Any) -> str:
    """Best-effort base URL of an SDK client (OpenAI and Anthropic shapes)."""
    return str(getattr(client, "base_url", None) or getattr(client, "_base_url", None) or "")


@contextlib.contextmanager
def hold(gate: threading.BoundedSemaphore | None, cancel_ref: Any = None) -> Iterator[None]:
    """Hold *gate* for the duration of the block.

    Waits in short polls so a call abandoned by its deadline (``cancel_ref``
    duck-typed on ``.aborted``, as :class:`~pebble.core.deadline.StreamAbortRef`)
    leaves the queue instead of later taking a slot for a result nobody reads.
    """
    if gate is None:
        yield
        return
    while not gate.acquire(timeout=_POLL_SECONDS):
        if getattr(cancel_ref, "aborted", False):
            raise GateAbandonedError("abandoned while waiting for a backend slot")
    try:
        if getattr(cancel_ref, "aborted", False):
            raise GateAbandonedError("abandoned while waiting for a backend slot")
        yield
    finally:
        gate.release()
