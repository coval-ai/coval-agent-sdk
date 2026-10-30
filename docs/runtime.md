# Runtime Contracts

These contracts describe the implemented client. They are the boundaries an adapter and any transport refactor must preserve.

## Connection lifecycle

`CovalVoiceClient.connect(simulation_id)` yields a ready `AudioConnection` through an async context manager. `run(config, on_connection)` launches a hosted run and invokes an async application handler for each eligible ready connection. The SDK has no agent classes or factories and does not construct or close an application's pipeline. A hosted agent fixture ID remains part of run configuration.

The SDK receives relay frames in its own task, independently of application processing. Each connection requires one validated `session_ready` before input, output, or normal completion. Applications consume `receive_audio()` and await `send_audio()` independently. `wait_closed()` observes voice completion without consuming input. The application owns state isolation, provider/pipeline tasks and their cleanup; stop them and end conversation spans before leaving the context or returning from the run handler.

A handler returning or a context exiting before voice completion fails visibly. A failed transport cancels and awaits a run handler so its finally blocks execute. Normal completion wakes receive iterators and pending sends; handlers may then finish their application cleanup. A direct context does not cancel the caller's task: connection reads/writes/wait_closed surface failure, and context exit checks the outcome even if the application ignored it. A handler must observe the connection and remain bounded by application policy or the run deadline.

All SDK-owned failures derive from `CovalError`. Application exceptions and cancellation propagate unchanged after cleanup; only SDK failures receive a `RunError` snapshot after launch. Trace cleanup must not hide the primary error. `connection.result` remains None until the context has successfully finalized; successful local voice transport does not imply hosted evaluation completion.

## Audio and backpressure

The wire format is PCM16 little-endian, one channel, 16 kHz. A sample contains two bytes; a normal 20 ms frame contains 640 bytes. The SDK validates even-length bytes and slices outgoing audio into paced frames, retaining pacing across calls and serializing simultaneous writes. It does not resample arbitrary application formats.

Each send accepts at most 2 MiB and must be awaited; the SDK does not buffer an unbounded stream of output. A send interrupted by normal voice completion returns False, including after a partial write; sent bytes are counted individually and never replayed. A failed connection or malformed output raises. Applications must bound their own queues and tasks. Only one receive iterator may be active per connection.

Unread input is bounded to 64 KiB across queued chunks. Input is acknowledged after validation and bounded enqueueing; overflowing the local budget fails the session instead of dropping audio or blocking terminal control frames. The hosted relay also limits unacknowledged input to 64 KiB. An overload close remains a failure. Normal completion drains queued input; failures wake receivers immediately rather than appearing as end-of-stream.

The SDK does not implement relay clear/barge-in controls. Concurrent input and output do not establish interruption support.

## Discovery and connection

Every API request uses the Coval API SDK through a private adapter and carries the configured API key and optional workspace selector. The adapter disables automatic transport retries, translates dependency failures into agent SDK errors, and releases each request's connection pool. API roots must use HTTP or HTTPS; relay offers must use WSS. The one-use relay token is sent in the WebSocket upgrade's `Authorization` header, not in ordinary diagnostics.

Offer discovery retries documented temporary HTTP/network failures and waits while an attempt is pending, provisioning, or expired and awaiting replacement. A ready offer must contain a secure URL, a nonempty token, and a future expiry. Authentication/permanent failures, unknown states, and attempts that ended before attachment fail visibly.

A live offer may retry network/timeouts and HTTP 500/502/503/504 handshake failures before it is consumed or expires, using the WebSocket library's retry classification. Each upgrade is capped by the remaining connection deadline, offer lifetime, and HTTP timeout. Authentication and other permanent rejections fail immediately. A consumed join is not replayed. A disconnected conversation needs a new simulation attempt rather than an automatic reconnect that could duplicate work.

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

`RunConfig.run_timeout_seconds` changes the batch monitoring deadline. This includes queued conversations and evaluation postprocessing. Polling sleeps are capped by the remaining batch deadline. An expired deadline fails visibly and cancels local sessions; it does not cancel the hosted run. The HTTP launch request has its own request timeout and is never automatically repeated.

Monitoring paginates simulations, defers PENDING/IN QUEUE conversations without starting their per-session connection deadline, attaches each eligible conversation once, and observes the run's terminal status. Queued conversations remain covered by the batch deadline and cannot be omitted from a successful result. An SDK-owned session failure is retained while monitoring continues to attach and serve unrelated conversations. Once the batch reaches a terminal outcome and all attached sessions finish, the first session failure raises `RunError` with completed sessions and per-session failures. Authentication/authorization failures, global monitoring errors, application exceptions, cancellation and the batch deadline cancel and await unfinished local handlers and transports. A completed batch requires a successful run outcome and no conversations missed before attachment; successful dispatch or one completed voice session does not establish that result.

## Relay closure and authoritative completion

A session requires one valid `session_ready` before `session_end`; repeated readiness and malformed control JSON fail as SDK protocol errors. Normally the relay sends `session_end`. If the connection closes normally after readiness without that frame, the SDK gives the simulation API up to ten seconds to confirm `COMPLETED`. Failed, cancelled, malformed, unavailable, and still-pending outcomes remain errors. Closure before readiness cannot claim completion.

Abnormal transport losses, failed close handshakes, and explicit protocol or overload rejections remain errors even if a later API read could report completion. A transport failure without a received close frame cannot use completion confirmation. The confirmation path does not reconnect or launch another conversation.

## Trace lifecycle

Tracing is optional and requires the `[tracing]` extra. Import the concrete exporter from `coval_agent_sdk.tracing`. Share one `CovalTraceExporter` between the pipeline's OpenTelemetry setup and the SDK client, and correlate the conversation root with the simulation ID. The application must finish its spans before leaving the context or returning from the handler so cleanup can ask the exporter to flush and finalize that conversation. The core uses a dependency-free protocol and does not look up an OpenTelemetry provider itself.

The exporter buffers at most 10,000 ended spans and 10,000 trace metadata entries by default. Tune `max_buffered_spans` and `max_buffered_traces` for the process. Damaged traces retain rejection information so later roots cannot turn an incomplete trace into a successful upload. Failed-session cleanup releases its spans and retains bounded rejection metadata for that trace, preserving overlapping conversations. Uploading spans remain counted against the buffer budget.

Completed conversations are uploaded individually as OTLP protobuf through the private Coval API transport. Each HTTP upload is a single attempt with the normal request timeout; failures return diagnostics without dependency logging or automatic replay. Failed conversations close, flush, and discard their spans. A flush or upload failure from the SDK's OpenTelemetry integration after a completed voice session is returned in `ConversationResult.trace.error` without rerunning the conversation or cancelling sibling calls; retained spans can be retried explicitly through the exporter. Application and session failures still propagate.

Prompts, transcripts, and model output may appear in spans. Enable export only when that data is intended for Coval, and keep test fixtures synthetic.

## Returned outcomes

After cleanup, a successful connection context makes its `ConversationResult` available through `connection.result`, including identity, local completion status, audio counts, and trace outcome. `run()` returns the run ID, hosted completion status, and completed conversation results when every observed session succeeds. A post-launch SDK run failure raises `RunError` carrying the known run snapshot, including completed `conversations` and `failures: tuple[ConversationFailure, ...]`. Failures retain the simulation ID, SDK exception type and diagnostic; unfinished or cancelled sessions have neither a success result nor a session-failure result. A session failure never becomes successful merely because the hosted status is COMPLETED. Its status is `None` until a monitoring response supplies one. The SDK emits no console output or log records; applications decide how to present results.
