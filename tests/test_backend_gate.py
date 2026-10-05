"""Per-backend concurrency gates for auxiliary LLM lanes (judge)."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from pebble.core.backend_gate import GateAbandonedError, backend_gate, client_base_url, hold
from pebble.core.deadline import StreamAbortRef


def test_unlimited_or_unknown_url_is_ungated():
    assert backend_gate("http://llm:8090/v1", 0) is None
    assert backend_gate("", 2) is None


def test_same_server_shares_one_gate():
    a = backend_gate("http://LLM:8090/v1/", 1)
    assert a is backend_gate("http://llm:8090/v1", 1)
    assert a is not backend_gate("http://other:8090/v1", 1)
    # Retuning the limit yields a fresh gate rather than a mis-sized one.
    assert a is not backend_gate("http://llm:8090/v1", 2)


def test_client_base_url_reads_sdk_shapes():
    assert client_base_url(SimpleNamespace(base_url="http://a/v1")) == "http://a/v1"
    assert client_base_url(SimpleNamespace(_base_url="http://b")) == "http://b"
    assert client_base_url(object()) == ""


def test_hold_bounds_concurrency():
    gate = backend_gate("http://bounded-test/v1", 2)
    active = 0
    peak = 0
    lock = threading.Lock()

    def work():
        nonlocal active, peak
        with hold(gate):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with lock:
                active -= 1

    threads = [threading.Thread(target=work) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak == 2


def test_abandoned_waiter_leaves_queue_without_taking_slot():
    gate = backend_gate("http://abandon-test/v1", 1)
    ref = StreamAbortRef()
    assert gate is not None
    gate.acquire()
    try:
        ref.abort()
        with pytest.raises(GateAbandonedError), hold(gate, ref):
            pytest.fail("abandoned call must not run")
    finally:
        gate.release()
    # The slot is free again: nothing leaked.
    assert gate.acquire(timeout=0.1)
    gate.release()


def test_hold_releases_on_error():
    gate = backend_gate("http://release-test/v1", 1)
    assert gate is not None
    with pytest.raises(ValueError), hold(gate):
        raise ValueError("boom")
    assert gate.acquire(timeout=0.1)
    gate.release()
