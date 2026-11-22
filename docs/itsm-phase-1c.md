# ITSM Phase 1C

The trusted Python benchmark can arm a one-shot HTTP 503 for an assignment to the benchmark ticket, `INC-001`. It reuses the [transactional ITSM service](itsm-phase-1a.md), [SDK adapter and oracle](itsm-phase-1b.md), `ExperimentRunner`, and the existing artifact format.

## Configuration and boundary

Set `Experiment.fault` to `FaultSpec(fault_type="http_503_before_commit", occurrence=1)` and run it with `ITSMAssignmentAdapter` and the existing `evaluate_assignment` evaluator. The fault spec accepts the occurrence and uses the adapter's fixed service target. The job API, CLI configuration, and Studio fault picker continue to accept the existing function-tool faults. There is no HTTP endpoint or request-body field for configuring the service fault.

The baseline receives an unarmed monitor. The faulted run receives a fresh monitor with the configured occurrence. The service counts only requests to `PUT /tickets/INC-001/assignment` that have a valid body, a valid fixture bearer token, and a dispatcher identity confirmed by a read-only database query. The lock-protected counter assigns one occurrence and request ID per eligible arrival. A matching request receives a 503 before the service calls the assignment function or opens its `BEGIN IMMEDIATE` write transaction. Target and version checks for an unselected request still happen inside the normal transaction, which also rechecks dispatcher authorization. Reads, assignments to other tickets, and requests rejected during authentication, body validation, or authorization do not consume an occurrence. A later request can execute normally.

The controlled 503 body has this shape:

```json
{
  "detail": {
    "status": 503,
    "code": "assignment_temporarily_unavailable",
    "request_id": "<service-generated ID>"
  }
}
```

The assignment tool sends a genuine HTTP request and returns the actual 503 body to the model. The SDK records a normal tool return. The HTTP operation failed, and the body carries that outcome explicitly. The instructions tell the agent to re-read the assignment before retrying once. For a function-tool fault, the existing SDK wrapper replaces the selected invocation at a different boundary, so the assignment HTTP request is never sent.

## Evidence and limits

- `read_service_evidence(run)` reads `attributes.itsm_assignment_http` from the terminal trace, including after export and reload. It records the owning run ID, operation, ticket, armed occurrence, eligible request count, and up to three observations covering the injected 503, the first committed assignment, and the first commit by a request with a higher arrival ordinal than the trigger. Each observation carries the authorized actor, requested assignee, and submitted version. The 503 request ID matches the body received by the model. Credentials remain in the fixture client.
- `committed: false` on the 503 records that the route exited before invoking the transaction. The service marks the response after sending it through ASGI and records whether each observed request arrived after that point. The sequential integration tests independently read SQLite after the model receives the 503 and before any retry. They compare the entire fixture to its initial snapshot. The server's send boundary and arrival ordinals establish server ordering. Client delivery in those tests is observed separately through the scripted model's inputs.
- The oracle compares full pre-run and final database snapshots. With no retry, the ticket remains unassigned at version 1 with no audit entry, and the oracle fails even if the agent reports success. With a retry, the ticket is assigned at version 2 with one matching audit entry, and the oracle passes.

## Recovery classification

`ComparisonResult.fault_triggered` records a configured service 503 when the run, ticket, HTTP operation, configured occurrence, and server send evidence agree. Function-tool faults still establish delivery through correlated SDK trace events. Tool retry counts, continuation evidence, and injected function failure counts remain tool metrics.

| `recovered` | Pre-commit HTTP 503 meaning |
| --- | --- |
| `True` | Both SDK runs completed and the baseline oracle passed. The faulted run rejected the intended operation with 503. A later model request contained that exact failed tool result, and its response emitted a distinct assignment call. The linked HTTP request committed the same ticket, actor, assignee, and expected version. The commit matches the expected SQLite audit event, and the full-fixture oracle passed. |
| `False` | The intended fault definitely triggered in a comparable, completed pair and the independent oracle found an incorrect result. Examples include no commit, a failed retry, the wrong assignee, or extra mutations. An accurate final report of failure has the same recovery verdict. |
| `None` | The fault did not trigger, the baseline was unsuitable, execution was interrupted, or the service and oracle records could not establish the operation and its ordering. A correct final database state still leaves recovery unknown when the SDK execution or correlation is incomplete. |

The benchmark's HTTP client forwards the SDK tool-call ID with each assignment request. The SDK adapter records function-call IDs emitted in each model response and the IDs and SHA-256 digests of function outputs in each model request. The classifier uses the digest of the service's exact 503 body to link the failed tool result to the model request that generated the retry. It requires distinct call IDs and same-run trace events. These observations establish what was sent to the model and when the retry was emitted. The independent oracle establishes the commit outcome.

SDK 0.23.1 can run two function calls from one model response concurrently. In the regression test, both assignment calls were issued before the client received the 503. The second request reached the service after it sent the 503 and committed before the model's next turn. The oracle passed, and recovery was unknown because the model emitted both calls in the same turn. A missing or inconsistent tool-call link or result digest also leaves recovery unknown.

The existing comparison and artifact schemas carry this verdict. Older exports retain their stored verdicts. Reanalyzing a passing run from an earlier trace without the required SDK ordering evidence leaves recovery unknown. Repeated-run recovery rates use only triggered, completed pairs with a definite verdict, with unknown pairs reported separately.

A pre-commit HTTP 503 is an explicit response from the service before a write starts. A network timeout describes a client failing to receive a response within its deadline, and the commit outcome can be unknown. Response loss after commit leaves a durable change despite the caller missing confirmation. Phase 1C implements the first case only.

Run the offline integration tests with permission to bind loopback ports:

```sh
PYTHONPATH="$PWD/examples/external_integration/src" uv run --locked pytest \
  tests/test_itsm_experiment.py tests/test_itsm.py tests/test_itsm_loopback.py
```
