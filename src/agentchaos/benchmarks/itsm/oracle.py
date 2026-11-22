"""Read-only verification of the complete fixture's persisted business state."""

from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from sqlite3 import connect

from .database import AssignmentEvent, Ticket


@dataclass(frozen=True)
class State:
    users: tuple[tuple[str, str], ...]
    tickets: tuple[Ticket, ...]
    audit: tuple[AssignmentEvent, ...]


@dataclass(frozen=True)
class Verification:
    mismatches: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.mismatches


def snapshot(path: Path) -> State:
    """Read all three tables from one consistent SQLite snapshot."""
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(connect(uri, uri=True, autocommit=True)) as db:
        db.execute("BEGIN")
        return State(
            users=tuple(db.execute("SELECT id, role FROM users ORDER BY id")),
            tickets=tuple(
                Ticket(*row)
                for row in db.execute(
                    "SELECT id, subject, assignee_id, version FROM tickets ORDER BY id"
                )
            ),
            audit=tuple(
                AssignmentEvent(*row)
                for row in db.execute(
                    """
                    SELECT ticket_id, ticket_version, event_type, actor_id,
                           previous_assignee_id, assignee_id
                    FROM ticket_events ORDER BY ticket_id, ticket_version
                    """
                )
            ),
        )


def _compare(expected: State, actual: State) -> Verification:
    mismatches = []
    if actual.users != expected.users:
        mismatches.append("User identities or roles differ from the expected state")
    if actual.tickets != expected.tickets:
        mismatches.append("Ticket fields, assignees, or versions differ")
    if actual.audit != expected.audit:
        mismatches.append("Audit records are missing, unexpected, or incorrect")
    return Verification(tuple(mismatches))


def verify_assignment(
    path: Path, before: State, *, ticket_id: str, actor_id: str, assignee_id: str
) -> Verification:
    """Expect exactly one authorized assignment relative to the prior snapshot."""
    return verify_assignment_state(
        before,
        snapshot(path),
        ticket_id=ticket_id,
        actor_id=actor_id,
        assignee_id=assignee_id,
    )


def verify_assignment_state(
    before: State, after: State, *, ticket_id: str, actor_id: str, assignee_id: str
) -> Verification:
    """Grade the same captured snapshot used to report supporting evidence."""
    roles = dict(before.users)
    if roles.get(actor_id) != "dispatcher" or roles.get(assignee_id) != "technician":
        return Verification(
            ("Expected assignment has an unauthorized actor or target",)
        )

    for ticket in before.tickets:
        if ticket.id == ticket_id:
            break
    else:
        return Verification(("Expected ticket is missing from the prior state",))

    updated_ticket = replace(
        ticket, assignee_id=assignee_id, version=ticket.version + 1
    )
    event = AssignmentEvent(
        ticket_id=ticket_id,
        ticket_version=updated_ticket.version,
        event_type="assigned",
        actor_id=actor_id,
        previous_assignee_id=ticket.assignee_id,
        assignee_id=assignee_id,
    )
    expected = replace(
        before,
        tickets=tuple(
            updated_ticket if row == ticket else row for row in before.tickets
        ),
        audit=tuple(
            sorted(
                (*before.audit, event),
                key=lambda row: (row.ticket_id, row.ticket_version),
            )
        ),
    )
    return _compare(expected, after)


def verify_unchanged(path: Path, before: State) -> Verification:
    """Expect no persisted changes anywhere, for example after a rejected write."""
    return _compare(before, snapshot(path))
