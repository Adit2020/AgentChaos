"""Portable evidence for the fixed ITSM assignment task."""

from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from agentchaos.models import AgentRun, DomainModel, EventKind

from .database import AssignmentEvent
from .oracle import State, snapshot, verify_assignment_state
from .service_fault import AssignmentMonitor, ServiceFaultEvidence

TICKET_ID = "INC-001"
ACTOR_ID = "dispatcher"
ASSIGNEE_ID = "tech-b"
DEFAULT_TASK = f"Assign ticket {TICKET_ID} to technician {ASSIGNEE_ID}."
ASSIGNMENT_TOOL = "assign_ticket"
_EVIDENCE_KEY = "itsm_oracle"
_SERVICE_EVIDENCE_KEY = "itsm_assignment_http"
_TERMINAL_KINDS = {
    EventKind.RUN_COMPLETED,
    EventKind.RUN_FAILED,
    EventKind.RUN_CANCELLED,
}


class AssignmentState(DomainModel):
    assignee_id: str | None
    version: int = Field(ge=1, strict=True)


class OracleEvidence(DomainModel):
    """A verdict from SQLite and the observed state of the target ticket."""

    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1)
    ticket_id: Literal["INC-001"] = TICKET_ID
    expected_actor_id: Literal["dispatcher"] = ACTOR_ID
    expected_assignee_id: Literal["tech-b"] = ASSIGNEE_ID
    before: AssignmentState | None
    after: AssignmentState | None
    audit: tuple[AssignmentEvent, ...]
    passed: bool = Field(strict=True)
    mismatches: tuple[str, ...]

    @model_validator(mode="after")
    def check_verdict(self) -> "OracleEvidence":
        if self.passed != (not self.mismatches):
            raise ValueError("verdict and mismatches disagree")
        return self


def _assignment(state: State) -> AssignmentState | None:
    for ticket in state.tickets:
        if ticket.id == TICKET_ID:
            return AssignmentState(
                assignee_id=ticket.assignee_id, version=ticket.version
            )
    return None


def capture_evidence(
    run: AgentRun, path: Path, before: State, monitor: AssignmentMonitor
) -> AgentRun:
    after = snapshot(path)
    result = verify_assignment_state(
        before, after, ticket_id=TICKET_ID, actor_id=ACTOR_ID, assignee_id=ASSIGNEE_ID
    )
    evidence = OracleEvidence(
        run_id=run.run_id,
        before=_assignment(before),
        after=_assignment(after),
        audit=tuple(event for event in after.audit if event.ticket_id == TICKET_ID),
        passed=result.passed,
        mismatches=result.mismatches,
    )
    terminal = run.events[-1]
    enriched = terminal.model_copy(
        update={
            "attributes": {
                **terminal.attributes,
                _EVIDENCE_KEY: evidence.model_dump(mode="json"),
                _SERVICE_EVIDENCE_KEY: monitor.evidence().model_dump(mode="json"),
            }
        }
    )
    return run.model_copy(update={"events": [*run.events[:-1], enriched]})


def _read_terminal_evidence[T: DomainModel](
    run: AgentRun, key: str, evidence_type: type[T]
) -> T | None:
    if not run.events or run.events[-1].kind not in _TERMINAL_KINDS:
        return None
    try:
        return evidence_type.model_validate(run.events[-1].attributes[key])
    except (KeyError, ValidationError):
        return None


def read_evidence(run: AgentRun) -> OracleEvidence | None:
    """Read and validate the run's stored oracle verdict."""
    evidence = _read_terminal_evidence(run, _EVIDENCE_KEY, OracleEvidence)
    return evidence if evidence is not None and evidence.run_id == run.run_id else None


def read_service_evidence(run: AgentRun) -> ServiceFaultEvidence | None:
    """Read and validate the run's authoritative service observations."""
    evidence = _read_terminal_evidence(run, _SERVICE_EVIDENCE_KEY, ServiceFaultEvidence)
    return evidence if evidence is not None and evidence.run_id == run.run_id else None


def evaluate_assignment(run: AgentRun) -> bool | None:
    evidence = read_evidence(run)
    return evidence.passed if evidence is not None else None
