"""Classify recovery for the ITSM pre-transaction 503 from stored evidence."""

from hashlib import sha256

from agentchaos.models import AgentRun, EventKind

from .database import AssignmentEvent
from .evidence import (
    ACTOR_ID,
    ASSIGNEE_ID,
    ASSIGNMENT_TOOL,
    TICKET_ID,
    AssignmentState,
    OracleEvidence,
    read_evidence,
    read_service_evidence,
)
from .service_fault import (
    AssignmentIntent,
    CommittedAssignment,
    RejectedAssignment,
    ServiceFaultEvidence,
    unavailable_response,
)


def _successful_assignment(oracle: OracleEvidence) -> bool:
    if not oracle.passed or oracle.before is None:
        return False
    version = oracle.before.version + 1
    return oracle.after == AssignmentState(
        assignee_id=ASSIGNEE_ID, version=version
    ) and oracle.audit == (
        AssignmentEvent(
            ticket_id=TICKET_ID,
            ticket_version=version,
            event_type="assigned",
            actor_id=ACTOR_ID,
            previous_assignee_id=oracle.before.assignee_id,
            assignee_id=ASSIGNEE_ID,
        ),
    )


def _matches_audit(commit: CommittedAssignment, oracle: OracleEvidence) -> bool:
    intent = commit.intent
    return (
        intent is not None
        and commit.resulting_version == intent.expected_version + 1
        and any(
            row.ticket_id == TICKET_ID
            and row.event_type == "assigned"
            and row.ticket_version == commit.resulting_version
            and row.actor_id == intent.actor_id
            and row.assignee_id == intent.assignee_id
            for row in oracle.audit
        )
    )


def _consistent_commits(service: ServiceFaultEvidence, oracle: OracleEvidence) -> bool:
    trigger = service.injected_response
    assert trigger is not None
    first, later = service.first_commit, service.first_commit_after_trigger
    if first is None:
        return later is None and not oracle.audit
    if first.occurrence > trigger.occurrence and first != later:
        return False
    if later is not None and (
        later.occurrence <= trigger.occurrence
        or later.occurrence < first.occurrence
        or (
            later != first
            and (
                later.occurrence == first.occurrence
                or later.request_id == first.request_id
            )
        )
    ):
        return False
    return all(
        commit.run_id == service.run_id
        and commit.occurrence <= service.requests_received
        and commit.occurrence != trigger.occurrence
        and commit.request_id != trigger.request_id
        and _matches_audit(commit, oracle)
        for commit in (first, later)
        if commit is not None
    )


def _reactive_retry(
    run: AgentRun, trigger: RejectedAssignment, retry: CommittedAssignment
) -> bool:
    failed_id, retry_id = trigger.tool_call_id, retry.tool_call_id
    if not failed_id or not retry_id or failed_id == retry_id:
        return False
    if any(event.event_id != f"{run.run_id}:{event.sequence}" for event in run.events):
        return False

    turns = []
    results = []
    for call_id in (failed_id, retry_id):
        calls = [
            event
            for event in run.events
            if event.call_id == call_id and event.kind == EventKind.TOOL_CALL
        ]
        terminals = [
            event
            for event in run.events
            if event.call_id == call_id
            and event.kind in {EventKind.TOOL_RESULT, EventKind.TOOL_ERROR}
        ]
        origins = [
            event
            for event in run.events
            if event.kind == EventKind.MODEL_RESPONSE
            and isinstance(ids := event.attributes.get("output_tool_call_ids"), list)
            for emitted_id in ids
            if emitted_id == call_id
        ]
        if (
            len(calls) != 1
            or len(terminals) != 1
            or len(origins) != 1
            or calls[0].name != ASSIGNMENT_TOOL
            or terminals[0].kind != EventKind.TOOL_RESULT
            or terminals[0].attributes.get("success") is not True
            or terminals[0].attributes.get("injected") is True
            or not origins[0].sequence < calls[0].sequence < terminals[0].sequence
        ):
            return False
        turns.append(origins[0])
        results.append(terminals[0])

    failed_turn, retry_turn = turns
    if not retry_turn.call_id or failed_turn.call_id == retry_turn.call_id:
        return False
    requests = [
        event
        for event in run.events
        if event.kind == EventKind.MODEL_REQUEST and event.call_id == retry_turn.call_id
    ]
    if (
        len(requests) != 1
        or not results[0].sequence < requests[0].sequence < retry_turn.sequence
    ):
        return False

    outputs = requests[0].attributes.get("input_tool_outputs")
    if not isinstance(outputs, list):
        return False
    failures = [
        item
        for item in outputs
        if isinstance(item, dict) and item.get("call_id") == failed_id
    ]
    digest = sha256(unavailable_response(trigger.request_id).encode()).hexdigest()
    return len(failures) == 1 and failures[0].get("sha256") == digest


def classify_precommit_503(
    baseline: AgentRun, faulted: AgentRun, *, occurrence: int, completed: bool
) -> tuple[bool, bool | None]:
    """Return observed trigger and independently corroborated recovery verdict."""
    service = read_service_evidence(faulted)
    trigger = service.injected_response if service is not None else None
    if (
        service is None
        or trigger is None
        or service.ticket_id != TICKET_ID
        or service.armed_occurrence != occurrence
        or trigger.occurrence != occurrence
        or trigger.run_id != service.run_id
        or trigger.occurrence > service.requests_received
        or trigger.after_503_sent is not False
        or service.response_sent is not True
    ):
        return False, None

    control = read_evidence(baseline)
    oracle = read_evidence(faulted)
    if (
        not completed
        or control is None
        or oracle is None
        or oracle.before is None
        or oracle.before != control.before
        or not _successful_assignment(control)
    ):
        return True, None

    expected = AssignmentIntent(
        actor_id=ACTOR_ID,
        assignee_id=ASSIGNEE_ID,
        expected_version=oracle.before.version,
    )
    if trigger.intent != expected or not _consistent_commits(service, oracle):
        return True, None
    if not oracle.passed:
        return True, False

    retry = service.first_commit_after_trigger
    if (
        retry is not None
        and retry == service.first_commit
        and retry.after_503_sent is True
        and retry.intent == expected
        and _successful_assignment(oracle)
        and _reactive_retry(faulted, trigger, retry)
    ):
        return True, True
    return True, None
