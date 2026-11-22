"""SQLite fixture and transactional ticket assignment."""

import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from secrets import token_urlsafe
from tempfile import TemporaryDirectory
from types import MappingProxyType

_SCHEMA = """
CREATE TABLE users (
    id TEXT PRIMARY KEY NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('dispatcher', 'technician'))
);
CREATE TABLE tickets (
    id TEXT PRIMARY KEY NOT NULL,
    subject TEXT NOT NULL,
    assignee_id TEXT REFERENCES users(id),
    version INTEGER NOT NULL CHECK (version >= 1)
);
CREATE TABLE ticket_events (
    ticket_id TEXT NOT NULL REFERENCES tickets(id),
    ticket_version INTEGER NOT NULL CHECK (ticket_version >= 2),
    event_type TEXT NOT NULL CHECK (event_type = 'assigned'),
    actor_id TEXT NOT NULL REFERENCES users(id),
    previous_assignee_id TEXT REFERENCES users(id),
    assignee_id TEXT NOT NULL REFERENCES users(id),
    PRIMARY KEY (ticket_id, ticket_version)
);
"""
_USERS = (
    ("dispatcher", "dispatcher"),
    ("tech-a", "technician"),
    ("tech-b", "technician"),
)
_TICKETS = (
    ("INC-001", "Printer queue stalled", None, 1),
    ("INC-002", "Shared calendar unavailable", "tech-a", 1),
    ("INC-003", "Headset microphone inactive", "tech-b", 1),
)


@dataclass(frozen=True)
class Ticket:
    id: str
    subject: str
    assignee_id: str | None
    version: int


@dataclass(frozen=True)
class AssignmentEvent:
    ticket_id: str
    ticket_version: int
    event_type: str
    actor_id: str
    previous_assignee_id: str | None
    assignee_id: str


@dataclass(frozen=True)
class Environment:
    database: Path
    tokens: Mapping[str, str] = field(repr=False)


@contextmanager
def isolated_environment() -> Iterator[Environment]:
    """Create a fresh fixture and remove it when the environment closes."""
    with TemporaryDirectory(prefix="agentchaos-itsm-") as directory:
        path = Path(directory) / "itsm.sqlite3"
        with closing(_open_writable(path, mode="rwc")) as db, db:
            db.executescript("BEGIN;\n" + _SCHEMA)
            db.executemany("INSERT INTO users (id, role) VALUES (?, ?)", _USERS)
            db.executemany(
                "INSERT INTO tickets (id, subject, assignee_id, version) "
                "VALUES (?, ?, ?, ?)",
                _TICKETS,
            )
        tokens = MappingProxyType({user_id: token_urlsafe(32) for user_id, _ in _USERS})
        yield Environment(path, tokens)


def _open_writable(path: Path, *, mode: str = "rw") -> sqlite3.Connection:
    db = _connect(path, mode=mode)
    db.execute("PRAGMA foreign_keys = ON")
    return db


def _connect(path: Path, *, mode: str) -> sqlite3.Connection:
    return sqlite3.connect(
        f"{path.resolve().as_uri()}?mode={mode}",
        uri=True,
        autocommit=sqlite3.LEGACY_TRANSACTION_CONTROL,
        isolation_level=None,
    )


class Rejection(Enum):
    UNKNOWN_TICKET = "Ticket not found"
    FORBIDDEN = "Only dispatchers can assign tickets"
    INVALID_ASSIGNEE = "Assignee must be an existing technician"
    STALE_VERSION = "Ticket version does not match"


class OperationRejected(Exception):
    def __init__(self, reason: Rejection) -> None:
        super().__init__(reason.value)
        self.reason = reason


def _get_ticket(db: sqlite3.Connection, ticket_id: str) -> Ticket:
    row = db.execute(
        "SELECT id, subject, assignee_id, version FROM tickets WHERE id = ?",
        (ticket_id,),
    ).fetchone()
    if row is None:
        raise OperationRejected(Rejection.UNKNOWN_TICKET)
    return Ticket(*row)


def get_ticket(path: Path, ticket_id: str) -> Ticket:
    with closing(_connect(path, mode="ro")) as db:
        return _get_ticket(db, ticket_id)


def get_history(path: Path, ticket_id: str) -> list[AssignmentEvent]:
    with closing(_connect(path, mode="ro")) as db, db:
        db.execute("BEGIN")
        _get_ticket(db, ticket_id)
        rows = db.execute(
            """
            SELECT ticket_id, ticket_version, event_type, actor_id,
                   previous_assignee_id, assignee_id
            FROM ticket_events WHERE ticket_id = ? ORDER BY ticket_version
            """,
            (ticket_id,),
        )
        return [AssignmentEvent(*row) for row in rows]


def _require_dispatcher(db: sqlite3.Connection, actor_id: str) -> None:
    actor = db.execute("SELECT role FROM users WHERE id = ?", (actor_id,)).fetchone()
    if actor != ("dispatcher",):
        raise OperationRejected(Rejection.FORBIDDEN)


def require_assignment_actor(path: Path, actor_id: str) -> None:
    """Authorize a request before reaching the pre-transaction fault boundary."""
    with closing(_connect(path, mode="ro")) as db:
        _require_dispatcher(db, actor_id)


def assign_ticket(
    path: Path, ticket_id: str, actor_id: str, assignee_id: str, expected_version: int
) -> Ticket:
    with closing(_open_writable(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        _require_dispatcher(db, actor_id)
        ticket = _get_ticket(db, ticket_id)
        assignee = db.execute(
            "SELECT role FROM users WHERE id = ?", (assignee_id,)
        ).fetchone()
        if assignee != ("technician",):
            raise OperationRejected(Rejection.INVALID_ASSIGNEE)

        next_version = ticket.version + 1
        updated = db.execute(
            "UPDATE tickets SET assignee_id = ?, version = ? "
            "WHERE id = ? AND version = ?",
            (assignee_id, next_version, ticket_id, expected_version),
        )
        if updated.rowcount != 1:
            raise OperationRejected(Rejection.STALE_VERSION)
        db.execute(
            """
            INSERT INTO ticket_events (ticket_id, ticket_version, event_type,
                                       actor_id, previous_assignee_id, assignee_id)
            VALUES (?, ?, 'assigned', ?, ?, ?)
            """,
            (ticket_id, next_version, actor_id, ticket.assignee_id, assignee_id),
        )
        return _get_ticket(db, ticket_id)
