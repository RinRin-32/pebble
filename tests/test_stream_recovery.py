"""Main-loop model-turn recovery (``ChatSession._request_turn``).

Regression suite for a workstream that died with ``APIError: Context size has
been exceeded.``: llama.cpp reported its shared KV cache filling DURING decode
as a mid-stream error, which the request-time-only overflow backstop never saw
and which the main loop never retried.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tests._session_helpers import make_session


class APIError(Exception):
    """Stand-in whose class NAME matches the OpenAI SDK's retryable APIError."""


class BadRequestError(Exception):
    """Stand-in for a non-retryable request-time rejection."""


KV_FULL = "Context size has been exceeded."
REPLY = {"role": "assistant", "content": "done"}


@pytest.fixture
def session(tmp_db, mock_openai_client):
    s = make_session(client=mock_openai_client, context_window=10_000, max_tokens=1_000)
    # No real sleeping in backoff; cancellation semantics are covered elsewhere.
    s._backoff_or_cancelled = lambda delay, my_generation=0: None  # type: ignore[method-assign]
    return s


def _run(session, stream_effects: list[Any], *, create_effects: list[Any] | None = None):
    """Drive ``_request_turn`` with scripted create/stream outcomes."""
    creates = iter(create_effects or [])

    def fake_create(msgs):
        nxt = next(creates, None)
        if isinstance(nxt, BaseException):
            raise nxt
        return iter(())

    streams = iter(stream_effects)

    def fake_stream(stream, gen):
        nxt = next(streams)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt

    with (
        patch.object(session, "_create_stream_with_retry", side_effect=fake_create) as create,
        patch.object(session, "_stream_response", side_effect=fake_stream),
        patch.object(session, "_compact_messages", return_value=True) as compact,
        patch.object(session, "_prepare_wire_messages", side_effect=lambda m: m),
        patch.object(session, "_full_messages", return_value=[{"role": "user", "content": "x"}]),
    ):
        result = session._request_turn([], session._generation)
    return result, create, compact


def test_mid_stream_retryable_error_is_retried(session):
    (msg, _), create, compact = _run(session, [APIError("connection reset"), REPLY])
    assert msg == REPLY
    assert create.call_count == 2
    compact.assert_not_called()


def test_kv_pool_exhaustion_backs_off_without_compacting(session):
    """llama.cpp's shared KV pool filled by a concurrent request: the same
    prompt succeeds once the pool drains, so wait rather than compact."""
    (msg, _), create, compact = _run(session, [APIError(KV_FULL), REPLY])
    assert msg == REPLY
    assert create.call_count == 2
    compact.assert_not_called()


def test_persistent_kv_exhaustion_compacts_once(session):
    (msg, msgs), create, compact = _run(
        session, [APIError(KV_FULL), APIError(KV_FULL), APIError(KV_FULL), REPLY]
    )
    assert msg == REPLY
    compact.assert_called_once()
    assert create.call_count == 4
    assert msgs == [{"role": "user", "content": "x"}]  # re-prepared after compaction


def test_kv_exhaustion_retries_are_bounded(session):
    errors = [APIError(KV_FULL)] * (session._STREAM_RETRIES + 2)
    with pytest.raises(APIError):
        _run(session, [*errors, REPLY])


def test_mid_stream_overflow_compacts_then_retries(session):
    (msg, _), _, compact = _run(
        session, [APIError("This model's maximum context length is 8192 tokens"), REPLY]
    )
    assert msg == REPLY
    compact.assert_called_once()


def test_overflow_persisting_after_compaction_raises(session):
    """A prompt still too large after compacting is deterministic — no backoff."""
    over = APIError("maximum context length is 8192")
    with pytest.raises(APIError):
        _run(session, [over, over, REPLY])


def test_structured_llama_overflow_calibrates_window(session):
    err = BadRequestError("request exceeds limits")
    err.body = {"error": {"type": "exceed_context_size_error", "n_ctx": 6_000}}  # type: ignore[attr-defined]
    (msg, _), _, compact = _run(session, [REPLY], create_effects=[err])
    assert msg == REPLY
    compact.assert_called_once()
    assert session.context_window == 6_000


def test_request_time_overflow_still_compacts(session):
    (msg, _), create, compact = _run(
        session, [REPLY], create_effects=[BadRequestError("maximum context length is 8192")]
    )
    assert msg == REPLY
    compact.assert_called_once()
    assert create.call_count == 2


def test_request_time_non_overflow_error_not_retried_again(session):
    """``_try_stream`` already retried request-time failures; don't double up."""
    with pytest.raises(APIError):
        _run(session, [REPLY], create_effects=[APIError("server unavailable")])


def test_non_retryable_mid_stream_error_raises(session):
    with pytest.raises(ValueError):
        _run(session, [ValueError("bad chunk"), REPLY])


def test_mid_stream_retries_are_bounded(session):
    errors = [APIError("reset")] * (session._STREAM_RETRIES + 1)
    with pytest.raises(APIError):
        _run(session, [*errors, REPLY])


def test_superseded_generation_does_not_retry(session):
    def stream_then_supersede(stream, gen):
        session._generation += 1
        raise APIError("reset")

    with (
        patch.object(session, "_create_stream_with_retry", return_value=iter(())),
        patch.object(session, "_stream_response", side_effect=stream_then_supersede) as sr,
        pytest.raises(APIError),
    ):
        session._request_turn([], session._generation)
    assert sr.call_count == 1
