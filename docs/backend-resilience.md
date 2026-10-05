# Backend resilience

How a session survives a model server that fails partway through a turn, and
how to size a local server so it fails less often.

## What used to happen

A long session on llama.cpp (`-c 262144`, default `--parallel`) died with
`state=error` and `last_error = APIError: Context size has been exceeded.`
Three things combined:

1. **A shared KV pool.** With auto `--parallel`, llama.cpp runs 4 slots over
   **one** unified KV cache. Each slot may grow to the full `-c`, but together
   they share it. The main loop's ~200k-token prompt, plus judge calls running
   on the same server, filled the pool mid-decode. llama.cpp then fails every
   in-flight slot with a `server_error` whose message is
   `Context size has been exceeded.` On an open stream the OpenAI SDK raises it
   as a bare `APIError`.
2. **No mid-stream retry.** The main loop retried failures only when the
   request was sent (`_try_stream`). A failure while the stream was being read
   went straight to the fatal path.
3. **The wording wasn't recognised.** `_is_ctx_overflow` didn't know llama.cpp's
   phrasing, so the overflow compact-and-retry never ran either.

## The recovery ladder

`ChatSession._request_turn` wraps every main-loop model turn, both sending the
request and reading the stream:

| Failure | Detected by | Response |
|---|---|---|
| Context overflow (request time or mid-stream) | `_is_ctx_overflow`: phrase list plus structured bodies (`exceed_context_size_error`, `context_length_exceeded`) | Compact once and retry. If the error body reports the server's real `n_ctx`, `context_window` is lowered to it, so the pre-send check catches the next overflow itself. |
| KV pool exhausted | `_is_kv_pool_exhausted`: llama.cpp's `Context size has been exceeded.` | Back off and retry, because the pool drains when the other requests finish. If it fails again after `_KV_RETRIES_BEFORE_COMPACT` (2) retries, compact once, in case this session is what's filling it. |
| Transport death mid-stream | A class listed in `provider.retryable_error_names`, raised after the stream opened | Back off and retry. |
| Request-time failure | (already handled) | `_try_stream` retries, then the fallback chain. This step does not retry it again. |

Backoff is about 4s, 8s, 16s, 32s with jitter (`_STREAM_RETRIES = 4`). Pressing
Stop ends it immediately. A superseded generation never retries. An overflow
that is still there after compacting can't be fixed by retrying, so it raises
right away.

## Judge fan-out gate

Each node caps how many judge evaluations it sends to one inference server at
once with `judge.max_concurrent_per_backend` (default 2, `0` = unlimited).
Other evaluations wait their turn, and the wait counts against `judge.timeout`.
The main loop is never gated. See `pebble/core/backend_gate.py`.

The cap is per process. Six nodes against one server can still send up to
6 × the cap.

## Sizing a local llama.cpp server

Retries only help with contention. They can't fix a server whose slots promise
more context than its pool holds. Choose one of these:

- **One slot, full context:** `--parallel 1 -c 262144`. Requests queue on the
  server instead of competing for cache. This is the simplest choice when one
  person drives a few sessions.
- **N slots, each guaranteed its share:** `--parallel N --kv-unified-per-slot W`
  caps each slot at W tokens, so the pool holds N × W. Then set the model's
  `context_window` in pebble to W, not the model's training length. Pebble's
  pre-send compaction keys off `context_window`.
- **Give the judge its own server.** Point `judge.model` at a small, separate
  model so approval checks never compete with the main loop for KV cache.
