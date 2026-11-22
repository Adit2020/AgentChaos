# AgentChaos Studio

React + TypeScript + Vite frontend for artifact inspection and trusted experiment jobs.
Node.js 22.12+ (or 24+) and npm are recommended.

From the repository root, start the backend in one terminal:

```sh
uv run --extra api --extra openai uvicorn --app-dir src agentchaos.api:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
```

The backend uses `artifacts/` by default. Set `AGENTCHAOS_ARTIFACT_ROOT` before
launching it to read another directory. No model API key is needed for reading.
Set `OPENAI_API_KEY` in the server environment before a live Playground submission.
The browser never receives or sends that key. Submitting may incur real model
API charges; startup, navigation, and scenario discovery never submit jobs.

In another terminal:

```sh
cd frontend
npm ci
npm run dev
```

Open `http://127.0.0.1:5173`. Vite forwards `/api` to `http://127.0.0.1:8000`
without rewriting paths. `changeOrigin: false` preserves the frontend Host
(including port 5173) alongside the browser Origin. The backend can therefore
compare the two and enforce its loopback Host and same-origin checks. Do not
change the Host to port 8000 or rewrite Origin to bypass validation. The backend
needs no CORS changes. Both servers bind
to localhost. Hash routes such as `#/exports/EXPORT_ID` support direct links and
browser back/forward navigation without server-side SPA routing.

## Scope and structure

- `src/api/contracts.ts`: Zod schemas and inferred TypeScript types matching
  the full FastAPI response and Python domain models. Invalid responses show
  an error instead of silently supplying missing metrics.
- `src/api/client.ts`, `src/hooks/useResource.ts`: same-origin fetch, typed errors,
  cancellation, stale-response protection, and retry.
- `src/pages/`: searchable/filterable history, single-run inspection, and a
  multi-run reliability analytics workspace.
- `src/pages/MultiRunInspection.tsx`: report-denominator-preserving analytics,
  small CSS bar charts, per-repetition metrics, and expandable traces.
- `src/components/`: independent event timelines, recorded outputs, status
  indicators, metric cards, and loading/empty/error states.
- `src/styles.css`: shared dark design tokens and responsive layouts.
- `src/test/fixtures/`: synthetic **test-only** responses produced through the
  existing artifact writer, readers, and FastAPI TestClient. They are never
  imported by the application or used as fallback data.

The history uses manifest metadata. Single-run details show recorded events,
outputs, and comparison fields. Multi-run details visualize only report
aggregates and repetition outcomes supplied by the API. Rates retain their
report denominators; incomplete and not-started work is excluded from recorded
outcomes, and null recovery remains unknown. Recovery true, false, and null
remain distinct.
Continuation is shown separately. Call correlation is scoped to each run;
trace columns are not synchronized or presented as exact cross-run matches.
Event details show only available metadata. Simulated timeouts are labeled.
Artifact text is rendered through React text nodes, without HTML injection.
No artifact data is written to browser storage or logged by the application.

## Playground

`#/playground` loads `GET /api/scenarios` and offers only registered fault and
tool options. Tasks and fault payloads are defined on the server. Explicit cost
acknowledgment and a click on Run Experiment are required for `POST /api/jobs`.
No submission is automatically retried. After an uncertain POST response, check
history before starting another experiment: the server may already have
accepted it.

Accepted jobs open `#/playground/jobs/JOB_ID`. The URL recovers status after a
refresh while the server retains its in-memory record. Requests to
`GET /api/jobs/{job_id}` run serially, approximately two seconds after each
response while queued or running. Temporary connection errors retry without
resubmission; unmount aborts the check and clears its timer. Terminal states
stop automatic polling. A timed-out job may still be cancellation-pending;
use Check status to request another update. Leaving the page does not cancel
server execution. Missing jobs explain restart and retention limitations.

Saved export links are checked against the existing artifact endpoint before
display. Completed jobs offer View Results; partial exports offer View partial
evidence and retain the failed/timed-out job status. Both open the existing
inspection page. No event streaming or multi-run submission is implemented.

## Verification and builds

```sh
npm run typecheck
npm test
npm run build
```

`npm run build` creates `dist/`. A deployed static frontend needs `/api` forwarded
to the backend on the same origin. Vite's proxy is for local development; this
phase does not configure production hosting or authentication. Artifact contents
may be sensitive. The API should remain local until access controls are added.
