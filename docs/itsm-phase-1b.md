# ITSM Phase 1B

The trusted `itsm_assignment` scenario assigns `INC-001` to `tech-b`. It uses the [Phase 1A HTTP service and fixture](itsm-phase-1a.md) and the existing `ExperimentRunner` and OpenAI Agents SDK adapter. The fixed assignment and its expected result are server-owned.

| Agent tool | HTTP request |
| --- | --- |
| `get_ticket(ticket_id)` | `GET /tickets/{ticket_id}` |
| `get_assignment(ticket_id)` | `GET /tickets/{ticket_id}/assignment` |
| `assign_ticket(ticket_id, assignee_id, expected_version)` | `PUT /tickets/{ticket_id}/assignment` |

The agent instructions call for reading the version before writing and rechecking state before retrying a temporary failure or a version conflict. Each run constructs fresh SDK tools with a client bound to its own loopback listener. Model-visible tool arguments carry the ticket, assignee, and version. The fixture dispatcher token is supplied by the client and stays out of tool responses, recorded evidence, and exports.

## Run and evidence lifecycle

`ExperimentRunner` runs the baseline and faulted executions sequentially. For each run the ITSM adapter creates a fresh fixture database, snapshots the starting state, and starts Uvicorn on an OS-assigned loopback port. The SDK then invokes the HTTP tools through the existing fault-capable adapter. The selected assignment fault replaces the targeted tool invocation before it can send HTTP. A later eligible invocation can make the real write.

The wrapper closes the client and waits for the listener to drain before it snapshots the database. It grades that snapshot with the Phase 1A oracle and attaches the verdict before deleting the temporary fixture. The listener and database are also cleaned up when execution fails or is cancelled. If the SDK supplied a terminal trace on an exception, the wrapper attaches the same evidence to that trace and re-raises the original exception.

The terminal event stores validated, versioned business-state evidence under `attributes.itsm_oracle`. It includes the run ID, expected ticket and assignee, observed assignment before and after the run, the ticket's audit rows, and the independent verdict with any mismatches. The oracle checks the full fixture. The export stores the target ticket's supporting evidence and any mismatch descriptions. Read it from a restored run with `read_evidence(run)` in `agentchaos.benchmarks.itsm.evidence`. The reader validates the evidence and checks its run ID. Missing or invalid evidence yields an unknown business-state verdict. The existing artifact format and older exports remain compatible.

The run status records SDK completion, `run.output` holds the agent's final answer, and the comparison tracks fault and retry evidence. A correct committed state with a later SDK error can have `itsm_oracle.passed = true` and `faulted_task_success = false`. The existing analyzer requires successful execution for task success and recovery. On a faulted SDK error the runner can attach the partial pair for export. If the baseline aborts before a pair exists, the terminal trace and evidence are available on the exception when the SDK provides that trace.

## Backend and offline verification

The backend lists the scenario through `GET /api/scenarios`. A job can select `itsm_assignment` and target `assign_ticket` with a supported tool fault. The job API requires explicit live-cost acknowledgment, and the adapter factory uses server-configured OpenAI credentials and the optional `AGENTCHAOS_MODEL` setting. The offline tests supply a scripted SDK `Model` and call the real SDK runner, TCP listener, and SQLite service. They do not contact a live provider.

Run the targeted tests from the repository root with permission to bind loopback sockets:

```sh
uv run --locked pytest tests/test_itsm_experiment.py tests/test_itsm.py tests/test_itsm_loopback.py
```

If the existing workspace example fails to import during full-suite collection, use its source directory on the test process's import path:

```sh
PYTHONPATH="$PWD/examples/external_integration/src" uv run --locked pytest
```
