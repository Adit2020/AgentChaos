# Local Studio API

The optional FastAPI service exposes completed JSON exports and can run one
baseline/faulted experiment using a trusted, server-registered scenario. Nothing
runs at startup or during discovery. Live execution requires an explicit cost
acknowledgment in every submission.

From the repository root, start it with:

```sh
uv run --extra api --extra openai uvicorn --app-dir src agentchaos.api:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
```

The default artifact root is `artifacts/` relative to the server's starting
directory. Set `AGENTCHAOS_ARTIFACT_ROOT` to another directory before launch, or
pass `artifact_root` to `create_app()` when embedding or testing the service.
FastAPI's interactive OpenAPI documentation is at `http://127.0.0.1:8000/docs`;
the schema is at `/openapi.json`.

Set `OPENAI_API_KEY` in the server environment before submitting a live job.
`AGENTCHAOS_MODEL` optionally selects the server's model; otherwise the SDK's
default is used, as in the order-support example. Never send credentials from
the browser. The `openai` extra is required for live execution; artifact reading
and scenario discovery also work with only the `api` extra.

## Endpoints and response contract

| Endpoint | Response |
| --- | --- |
| `GET /api/health` | `{"status":"ok"}` |
| `GET /api/exports` | `{"exports":[...]}` with each export's `export_id`, `artifact_type`, `exported_at`, and validated `manifest` |
| `GET /api/exports?artifact_type=experiment` | Only single-run exports |
| `GET /api/exports?artifact_type=multi_run` | Only multi-run exports |
| `GET /api/exports/{export_id}` | `{"export_id", "artifact_type", "artifact"}` with a parsed single-run or multi-run artifact |

The list is ordered by `exported_at` newest first, then by `export_id` ascending
for equal timestamps. Listing checks manifests and required file presence; it
does not parse run traces. A directory without `manifest.json` is unfinished
and is not listed. A corrupt completed export causes an error rather than a
partial list.

The detail response preserves the domain model fields. A single-run `artifact`
contains `manifest`, `baseline`, `faulted`, and `comparison`. A multi-run
`artifact` contains `manifest`, `summary`, and `repetitions`; each repetition
has its manifest `state` and an `outcome` for `recorded` entries. For
`incomplete` and `not_started`, `outcome` is `null`. Unknown evaluation and
recovery values remain `null`.

Errors use `{"error":{"code":"...","message":"..."}}`. Missing exports or
required files return 404, unsafe paths return 400, ambiguous IDs return 409,
and malformed or unsupported artifacts return 422. Messages do not include
local file paths or artifact contents.

This API has no authentication or cross-origin access configuration. Bind it
to `127.0.0.1` for local development. Artifacts can contain tasks, final
outputs, and trace attributes that are sensitive. Add authentication and a
reviewed access policy before any public deployment.

## Trusted scenarios and jobs

| Endpoint | Response |
| --- | --- |
| `GET /api/scenarios` | `{"scenarios":[...]}` with validated metadata only |
| `POST /api/jobs` | HTTP 202 and the initially `queued` job |
| `GET /api/jobs/{job_id}` | Current job state; HTTP 404 for unknown or expired IDs |

The initial registry contains `order_support`, based on the existing example.
Its lookup and deterministic answer evaluator are shared with that example.
The tool reads fixed local order data; the agent uses live OpenAI model calls.
The task is always `Where is order 123?`. The only target tool is
`get_order_status_json`; supported faults are `tool_error`, `tool_timeout`, and
`malformed_tool_output`. Occurrence is an integer from 1 to 3. A later selected
occurrence may never be reached; the resulting comparison preserves that fact.
The timeout fault remains the existing 10 ms simulated tool-boundary delay.
Error text and the malformed JSON payload are fixed on the server.

Example scenario metadata (the response has no factory or Python module path):

```json
{
  "scenario_id": "order_support",
  "name": "Order support",
  "description": "Look up a fixed local order and compare baseline versus faulted agent behavior. Uses live model calls.",
  "supported_target_tools": ["get_order_status_json"],
  "supported_fault_types": ["tool_error", "tool_timeout", "malformed_tool_output"],
  "default_task": "Where is order 123?",
  "max_occurrence": 3
}
```

Submitting the following JSON **starts a live, potentially billable experiment**:

```json
{
  "scenario_id": "order_support",
  "fault_type": "tool_error",
  "target_tool": "get_order_status_json",
  "occurrence": 1,
  "acknowledge_live_cost": true
}
```

`acknowledge_live_cost` is required and must be the JSON boolean `true`.
Unknown fields, arbitrary tasks, factories, code, paths, error text, and custom
fault parameters are rejected. There is no multi-run submission endpoint.

The initial response contract is:

```json
{
  "job_id": "<generated job ID>",
  "scenario_id": "order_support",
  "state": "queued",
  "submitted_at": "<UTC timestamp>",
  "started_at": null,
  "finished_at": null,
  "export_id": null,
  "partial_export": false,
  "cancellation_pending": false,
  "error": null
}
```

Poll the job endpoint using that ID. State transitions are `queued` → `running`
→ `completed`, `failed`, or `timed_out`; shutdown can also fail a queued job.
`completed` means the pair was produced and its artifacts saved, independently
of task success or recovery. Those scientific outcomes remain in the artifact.
The successful job's `export_id` can be passed directly to
`GET /api/exports/{export_id}`. The existing writer publishes `manifest.json`
last, so unfinished exports remain absent from experiment history.

Failures use a fixed, sanitized `error` object, for example:

```json
{"code":"execution_failed","message":"Experiment execution failed."}
```

If the runner attaches a partial baseline/faulted pair to an exception or
cancellation, the job exports it with `partial_export: true`, retaining the
`failed` or `timed_out` state. A manifest marks complete files, not a successful
task. One-sided traces cannot be exported by the existing pair writer, so
failures before a pair exists have `export_id: null`. Export failures also use
sanitized messages; no adapter exception text or stack trace is returned.

### Capacity, deadlines, and retention

There is one reserved execution slot and no waiting backlog. Excess submissions
return HTTP 429 with `job_capacity_reached`. The slot remains reserved while
artifacts are being written. Writes run off the API event loop.

The baseline/faulted execution deadline defaults to 120 seconds. On expiry, the
state becomes `timed_out` and cancellation is requested. While the coroutine
has not settled, `cancellation_pending` is `true`, `finished_at` is `null`, and
new submissions remain rejected. Cancellation is cooperative: this does not
prove the provider stopped processing or that billing stopped. A late result
may still be exported, but the job stays `timed_out`. Artifact I/O follows
execution and is outside the execution deadline.

Shutdown stops submissions and requests cancellation, waiting up to 5 seconds.
An adapter that suppresses cancellation can outlive that grace period; process
termination is the hard stop. Trusted factories and adapters must not block the
event loop. This service provides no process isolation or strict dollar/token
budget. A pair starts two agent executions, each potentially making several
model calls and SDK retries.

Only the latest 100 job records are retained, including the active job. Oldest
inactive records are evicted on submission; their saved artifacts remain on
disk. All job state is in memory and is lost on server restart. Use exactly one
Uvicorn worker: multiple workers each have independent capacity and job stores.
Avoid automatic reload during live jobs.

For embedding and offline testing, `create_app(artifact_root, scenarios=...,
job_settings=...)` accepts trusted Python scenario definitions and a
`agentchaos.jobs.JobSettings` instance. Limits are server-owned: execution
timeout is positive and at most 600 seconds, shutdown grace at most 30 seconds,
and retention between 1 and 1000 records. Defaults require no configuration.
Run application lifespan (for example, use `TestClient` as a context manager)
to enable submissions; otherwise they return HTTP 503.

### Local security boundary

Job submissions require `application/json`, a loopback Host, and, when an Origin
header is present, the same origin as the API request. Cross-origin browser
submissions are rejected with HTTP 403; no permissive CORS is enabled. Use a
same-origin development proxy for a future frontend. These checks help protect
the local browser boundary; they do not authenticate local programs.

Keep binding to `127.0.0.1`. Authentication, stronger isolation, request/resource
limits, and a reviewed deployment policy are required before public exposure.
Scenario IDs select only the explicit server registry and never invoke the
CLI's import-path loader. Hosted SDK tracing remains disabled through the
existing adapter. Artifacts may contain sensitive tasks, outputs, and trace
attributes: credential-field redaction is not a general secrets scrubber.
