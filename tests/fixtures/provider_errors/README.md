# Provider error fixtures

Captured with the OpenCode CLI v2.0.5 (`--standalone --format json --model stub/m1`)
against `stub.py <429|500|400> <port>` (OpenAI-compatible stub, sandboxed HOME/XDG).
The CLI retries internally (~40 s for 429), then emits ONE final error event and exits 1.

Sanitized shapes (`message` is unbounded provider text: never read it):

    {"type":"error","timestamp":<int>,"sessionID":"ses_...","error":{"type":"provider.rate-limit","message":"...","status":429}}
    {"type":"error","timestamp":<int>,"sessionID":"ses_...","error":{"type":"provider.internal","message":"...","status":500}}
    {"type":"error","timestamp":<int>,"sessionID":"ses_...","error":{"type":"provider.invalid-request","message":"...","status":400}}

Classify by the integer `error.status` (429 and 5xx retry; other 4xx do not) and the bounded `error.type` name.
