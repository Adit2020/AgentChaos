# ITSM Phase 1A

The benchmark provides a local SQLite fixture and one write operation, ticket assignment. The implementation lives under `src/agentchaos/benchmarks/itsm` and uses the repository's existing API dependencies.

## Run the service

From the repository root, with Python 3.12 or newer:

```sh
uv sync --locked
uv run --locked python -m agentchaos.benchmarks.itsm --port 8765
```

The launcher listens on `127.0.0.1` and disables proxy headers. It prints the path of a private `tokens.json` file containing the fixture bearer tokens keyed by user ID. Keep the listener on loopback when using the app factory in a custom test harness. Each operation below requires an `Authorization: Bearer <token>` header. Use the `dispatcher` token for writes and any fixture token for reads. The server maps the token to a fixture identity and reads the current role from SQLite inside the write transaction. The assignment body accepts only `assignee_id` and `expected_version`.

## API

| Method and path | Successful response |
| --- | --- |
| `GET /tickets/{ticket_id}` | Ticket with `id`, `subject`, `assignee_id`, and `version` |
| `GET /tickets/{ticket_id}/assignment` | Current `ticket_id`, `assignee_id`, and `version` |
| `PUT /tickets/{ticket_id}/assignment` | Updated assignment in the same shape as the assignment GET |
| `GET /tickets/{ticket_id}/audit` | Array of audit records in version order |

Send a PUT body such as `{"assignee_id":"tech-b","expected_version":1}` to assign `INC-001` to `tech-b`. All successful responses use HTTP 200. An unassigned ticket has a null `assignee_id`. Audit records contain `ticket_id`, `ticket_version`, `event_type`, `actor_id`, `previous_assignee_id`, and `assignee_id`.

| Status | Meaning |
| --- | --- |
| 401 | Missing or invalid token |
| 403 | Authenticated caller lacks the dispatcher role |
| 404 | Ticket does not exist |
| 409 | Expected version differs from the current version |
| 422 | Invalid request body or target is not an existing technician |
| 500 | Unexpected server error, including a database error |

## Schema and fixture

| Table | Fields and constraints |
| --- | --- |
| `users` | Text primary key `id` and required `role` restricted to dispatcher or technician |
| `tickets` | Text primary key `id`, required `subject`, nullable `assignee_id` referencing a user, and required integer `version` starting at 1 |
| `ticket_events` | Composite primary key `(ticket_id, ticket_version)`, fixed `event_type` of `assigned`, required actor and new assignee user references, and a nullable previous assignee user reference |

The seed contains one dispatcher and two technicians, `tech-a` and `tech-b`. Technicians can read tickets and be assigned work. Only the dispatcher can make assignments. `INC-001` starts unassigned, `INC-002` starts assigned to `tech-a`, and `INC-003` starts assigned to `tech-b`. All tickets start at version 1 with an empty audit history.

`isolated_environment()` creates a fresh private temporary directory and initializes its SQLite schema and fixture rows in one transaction. Each context has a separate database file and independently generated bearer tokens. Close the context to remove the files and open a new context to reset the fixture. The command-line launcher owns one context for the lifetime of the server. Keep each experiment execution's database available until the oracle has inspected it, or capture its verdict before closing the context.

## Assignment transaction

After bearer authentication and body validation, the service opens a SQLite connection with foreign keys enabled and starts `BEGIN IMMEDIATE`. Within that transaction it checks the actor's role, loads the ticket, and verifies that the target exists and has the technician role. It updates the assignee and increments the version in one UPDATE guarded by the submitted version. If the UPDATE matches no row, it returns a version conflict without inserting an event. Otherwise it inserts one audit event with the actor, old assignee, new assignee, and resulting version. It reads the updated ticket in the same transaction and commits before returning the HTTP response.

A failed check, write, or commit rolls back both the ticket update and the audit insert. SQLite serializes writers, so concurrent requests cannot both commit with the same expected version. A successful assignment of the current technician still increments the version and creates an event.

## Independent verification

Call `oracle.snapshot(environment.database)` before the task. After an expected assignment, call `oracle.verify_assignment(environment.database, before, ticket_id=..., actor_id=..., assignee_id=...)`. For an expected rejection, call `oracle.verify_unchanged(environment.database, before)`. Each returns a verdict with `passed` and `mismatches`.

The oracle opens its own read-only SQLite connection and reads all three tables in a single transaction. For a successful assignment it checks the expected actor and target roles, then compares the entire persisted state with the prior state plus one assignment. It verifies the assignee, version, complete audit event, absence of extra events, and absence of changes elsewhere in the fixture. For a rejection it requires the entire persisted fixture to match the prior snapshot. Its evidence is the database state before and after the task. It can detect changes still present at verification time.

A later integration can manage a separate fixture for each baseline and faulted execution at the adapter run boundary. Associate the database and pre-run snapshot, or the resulting verdict, with the recorder's run ID. The existing `TaskEvaluator` callback can return that run's oracle verdict when `ExperimentRunner` compares the pair. The harness can distinguish unavailable evidence from a verified mismatch using the evaluator's existing `None` result.

## Tests

```sh
uv run --locked pytest tests/test_itsm.py tests/test_itsm_loopback.py
uv run --locked pytest
uv run --locked ruff format --check .
uv run --locked ruff check .
```

`test_itsm_loopback.py` starts Uvicorn on an OS-assigned local TCP port in the test process, verifies authorization and a committed reassignment over HTTP, and shuts down the server in teardown. `test_itsm.py` sends requests in-process through ASGI while exercising real SQLite reads and writes. Its direct database checks cover rollback, persisted-state corruption, and fixture isolation. The tests use synthetic data and require permission to bind a loopback socket for the TCP case.
