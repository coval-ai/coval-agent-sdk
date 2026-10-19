# Runtime Contracts

These contracts describe the implemented client. They are the boundaries an adapter and any transport refactor must preserve.

## Agent lifecycle

The Python client receives a factory taking a simulation ID. Each local conversation constructs one fresh adapter, opens an outbound relay connection, waits for `session_ready`, calls `on_ready()`, and starts output only after that callback completes. Incoming audio is acknowledged before `on_audio()` is awaited.

`on_ready()` and `on_audio()` must return promptly. Put slow ASR, model, or TTS work into separately owned tasks and bounded queues. The receive loop cannot read the next frame while a callback is awaiting remote work. Streaming adapters yield output independently; callback adapters may return a greeting or reply directly.

The session cancels and awaits its output task before closing the adapter. The adapter's `close()` method must cancel and await its own provider/pipeline tasks and finish conversation spans. Session or adapter errors propagate after cleanup. A secondary cleanup error must not hide the primary failure.

## Audio and backpressure

The wire format is PCM16 little-endian, one channel, 16 kHz. A sample contains two bytes; a normal 20 ms frame contains 640 bytes. The SDK validates even-length audio, slices outgoing audio into paced frames, and retains pacing state across independently yielded chunks. It does not resample an arbitrary adapter format.

Callback replies are bounded by both a four-reply queue and a 2 MiB budget covering queued and in-flight output. Streaming output must yield `bytes` chunks no larger than 2 MiB. Empty chunks are ignored, malformed output fails the session, and an output iterator that ends before `session_end` fails the session.

The relay permits 64 KiB of unacknowledged input, approximately two seconds of this audio format. A callback that blocks reception can accumulate that backlog even though each received frame was acknowledged promptly. A relay overload close with code 1013 remains an error and tells the adapter author to move slow work off the receive loop. The budget is cumulative, not a fresh allowance for every callback.

The SDK does not implement relay clear/barge-in controls. An adapter must not claim interruption support merely because it consumes and produces audio concurrently.

## Discovery and connection

Every API request carries the configured API key and optional workspace selector. API roots must use HTTP or HTTPS; relay offers must use WSS. The one-use relay token is sent in the WebSocket upgrade's `Authorization` header, not in ordinary diagnostics.

Offer discovery retries documented temporary HTTP/network failures and waits while an attempt is pending, provisioning, or expired and awaiting replacement. A ready offer must contain a secure URL, a nonempty token, and a future expiry. Authentication/permanent failures, unknown states, and attempts that ended before attachment fail visibly.

A live offer may retry transient handshake failures before it is consumed or expires. A consumed join is not replayed. A disconnected conversation needs a new simulation attempt rather than an automatic reconnect that could duplicate work.

## Deadlines and run monitoring

| Boundary | Default |
| --- | --- |
| HTTP request timeout | 15 seconds |
| Offer/upgrade connection deadline | 300 seconds |
| Wait for the relay's readiness frame | 300 seconds |
| Monitoring-read retry budget | 30 seconds, capped by the run deadline |
| Batch monitoring deadline after launch | 3,600 seconds |
| Persisted completion confirmation after relay closure | 10 seconds |
| WebSocket ping interval / pong timeout | 20 seconds / 20 seconds |

`RunConfig.run_timeout_seconds` changes the batch monitoring deadline. This includes queued conversations and evaluation postprocessing. An expired deadline fails visibly and cancels local sessions; it does not cancel the hosted run. The HTTP launch request has its own request timeout and is never automatically repeated.

Monitoring paginates simulations, attaches each eligible conversation once, and observes the run's terminal status. A failed local session cancels remaining local sessions. A completed batch requires a successful run outcome and no conversations missed before attachment; successful dispatch or one completed voice session does not establish that result.

## Relay closure and authoritative completion

Normally the relay sends `session_end`. If the connection ends after readiness without that frame, the SDK gives the simulation API up to ten seconds to confirm `COMPLETED`. Failed, cancelled, malformed, unavailable, and still-pending outcomes remain errors. Closure before readiness cannot claim completion.

Explicit protocol and overload rejections remain errors even if a later API read could report completion. The confirmation path does not reconnect or launch another conversation.

## Trace lifecycle

Tracing is optional. Share one `CovalTraceExporter` between the pipeline's OpenTelemetry setup and the SDK client, and correlate the conversation root with the simulation ID. The agent must finish its spans before closure returns so the SDK can flush and finalize that conversation.

The exporter buffers at most 10,000 ended spans and 10,000 trace metadata entries by default. Tune `max_buffered_spans` and `max_buffered_traces` for the process. Damaged traces retain rejection information so later roots cannot turn an incomplete trace into a successful upload. Uploading spans remain counted against the buffer budget.

Completed conversations are uploaded individually. Failed conversations close, flush, and discard their spans. An upload failure after a completed voice session is reported separately without rerunning the conversation or cancelling sibling calls; retained spans can be retried explicitly through the exporter. Agent and session failures still propagate.

Prompts, transcripts, and model output may appear in spans. Enable export only when that data is intended for Coval, and keep test fixtures synthetic.
