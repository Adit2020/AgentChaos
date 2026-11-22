"""A one-shot pre-transaction 503 and bounded server-side assignment evidence."""

import json
from threading import Lock
from typing import Literal
from uuid import uuid4

from pydantic import Field

from agentchaos.models import DomainModel

UNAVAILABLE_CODE = "assignment_temporarily_unavailable"
ASSIGNMENT_OPERATION = "PUT /tickets/{ticket_id}/assignment"
TOOL_CALL_HEADER = "X-AgentChaos-Tool-Call-ID"


def unavailable_response(request_id: str) -> str:
    """The exact response text returned by the HTTP tool on an injected 503."""
    return json.dumps(
        {"detail": {"status": 503, "code": UNAVAILABLE_CODE, "request_id": request_id}},
        separators=(",", ":"),
    )


class AssignmentIntent(DomainModel):
    actor_id: str = Field(min_length=1)
    assignee_id: str = Field(min_length=1)
    expected_version: int = Field(ge=1, strict=True)


class AssignmentRequestRef(DomainModel):
    request_id: str = Field(min_length=1)
    occurrence: int = Field(ge=1, strict=True)
    run_id: str | None = Field(default=None, min_length=1)
    intent: AssignmentIntent | None = None
    after_503_sent: bool | None = Field(default=None, strict=True)
    tool_call_id: str | None = Field(default=None, min_length=1)


class RejectedAssignment(AssignmentRequestRef):
    status_code: Literal[503]
    committed: Literal[False]


class CommittedAssignment(AssignmentRequestRef):
    status_code: Literal[200]
    committed: Literal[True]
    resulting_version: int = Field(ge=2, strict=True)


class ServiceFaultEvidence(DomainModel):
    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    operation: Literal["PUT /tickets/{ticket_id}/assignment"]
    ticket_id: str = Field(min_length=1)
    armed_occurrence: int | None = Field(ge=1, strict=True)
    requests_received: int = Field(ge=0, strict=True)
    injected_response: RejectedAssignment | None
    response_sent: bool | None = Field(default=None, strict=True)
    first_commit: CommittedAssignment | None
    first_commit_after_trigger: CommittedAssignment | None


class AssignmentMonitor:
    """Own counters and observations for one fixture's target assignment route."""

    def __init__(
        self, ticket_id: str, *, run_id: str, fault_occurrence: int | None
    ) -> None:
        self.ticket_id = ticket_id
        self._run_id = run_id
        self._fault_occurrence = fault_occurrence
        self._lock = Lock()
        self._requests_received = 0
        self._injected: RejectedAssignment | None = None
        self._response_sent = False
        self._first_commit: CommittedAssignment | None = None
        self._first_commit_after_trigger: CommittedAssignment | None = None

    def receive(
        self, intent: AssignmentIntent, *, tool_call_id: str | None = None
    ) -> AssignmentRequestRef:
        with self._lock:
            self._requests_received += 1
            request_id = uuid4().hex
            occurrence = self._requests_received
            if occurrence == self._fault_occurrence:
                self._injected = RejectedAssignment(
                    request_id=request_id,
                    occurrence=occurrence,
                    run_id=self._run_id,
                    intent=intent,
                    after_503_sent=False,
                    tool_call_id=tool_call_id,
                    status_code=503,
                    committed=False,
                )
                return self._injected
            return AssignmentRequestRef(
                request_id=request_id,
                occurrence=occurrence,
                run_id=self._run_id,
                intent=intent,
                after_503_sent=self._response_sent,
                tool_call_id=tool_call_id,
            )

    async def mark_response_sent(self) -> None:
        """Run after the selected 503 response has been sent by the ASGI app."""
        with self._lock:
            self._response_sent = True

    def record_commit(self, request: AssignmentRequestRef, version: int) -> None:
        observed = CommittedAssignment(
            request_id=request.request_id,
            occurrence=request.occurrence,
            run_id=request.run_id,
            intent=request.intent,
            after_503_sent=request.after_503_sent,
            tool_call_id=request.tool_call_id,
            status_code=200,
            committed=True,
            resulting_version=version,
        )
        with self._lock:
            if (
                self._first_commit is None
                or request.occurrence < self._first_commit.occurrence
            ):
                self._first_commit = observed
            if (
                self._injected is not None
                and request.occurrence > self._injected.occurrence
                and (
                    self._first_commit_after_trigger is None
                    or request.occurrence < self._first_commit_after_trigger.occurrence
                )
            ):
                self._first_commit_after_trigger = observed

    def evidence(self) -> ServiceFaultEvidence:
        with self._lock:
            return ServiceFaultEvidence(
                run_id=self._run_id,
                operation=ASSIGNMENT_OPERATION,
                ticket_id=self.ticket_id,
                armed_occurrence=self._fault_occurrence,
                requests_received=self._requests_received,
                injected_response=self._injected,
                response_sent=self._response_sent,
                first_commit=self._first_commit,
                first_commit_after_trigger=self._first_commit_after_trigger,
            )
