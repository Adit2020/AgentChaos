"""ITSM integration tests using FastAPI's in-process ASGI transport."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from agentchaos.benchmarks.itsm.api import create_app
from agentchaos.benchmarks.itsm.database import isolated_environment
from agentchaos.benchmarks.itsm.oracle import (
    snapshot,
    verify_assignment,
    verify_unchanged,
)

ASSIGNMENT = "/tickets/INC-001/assignment"
PAYLOAD = {"assignee_id": "tech-b", "expected_version": 1}


@pytest.fixture
def environment():
    with isolated_environment() as env:
        yield env


@pytest.fixture
def client(environment):
    with TestClient(create_app(environment)) as http:
        yield http


def headers(environment, user_id="dispatcher"):
    return {"Authorization": f"Bearer {environment.tokens[user_id]}"}


def verify_inc001(environment, before):
    return verify_assignment(
        environment.database,
        before,
        ticket_id="INC-001",
        actor_id="dispatcher",
        assignee_id="tech-b",
    )


def test_ticket_and_assignment_views(client, environment):
    auth = headers(environment, "tech-a")
    ticket = client.get("/tickets/INC-001", headers=auth)
    assert ticket.status_code == 200
    assert ticket.json() == {
        "id": "INC-001",
        "subject": "Printer queue stalled",
        "assignee_id": None,
        "version": 1,
    }
    assignment = client.get(ASSIGNMENT, headers=auth)
    assert assignment.status_code == 200
    assert assignment.json() == {
        "ticket_id": "INC-001",
        "assignee_id": None,
        "version": 1,
    }
    audit = client.get("/tickets/INC-001/audit", headers=auth)
    assert audit.status_code == 200
    assert audit.json() == []
    for suffix in ("", "/assignment", "/audit"):
        assert client.get(f"/tickets/missing{suffix}", headers=auth).status_code == 404


@pytest.mark.parametrize(
    ("user_id", "ticket_id", "payload", "status_code"),
    [
        (None, "INC-001", PAYLOAD, 401),
        ("invalid-token", "INC-001", PAYLOAD, 401),
        ("tech-a", "INC-001", PAYLOAD, 403),
        ("tech-b", "INC-001", PAYLOAD, 403),
        ("tech-a", "INC-001", {**PAYLOAD, "actor_id": "dispatcher"}, 422),
        ("dispatcher", "INC-001", {**PAYLOAD, "assignee_id": "missing"}, 422),
        ("dispatcher", "INC-001", {**PAYLOAD, "assignee_id": "dispatcher"}, 422),
        ("dispatcher", "INC-001", {**PAYLOAD, "expected_version": "1"}, 422),
        ("dispatcher", "missing", PAYLOAD, 404),
    ],
)
def test_rejected_request_leaves_database_unchanged(
    client, environment, user_id, ticket_id, payload, status_code
):
    before = snapshot(environment.database)
    auth = {}
    if user_id is not None:
        token = environment.tokens.get(user_id, user_id)
        auth = {"Authorization": f"Bearer {token}"}
    response = client.put(
        f"/tickets/{ticket_id}/assignment", json=payload, headers=auth
    )
    assert response.status_code == status_code
    if status_code == 401:
        assert response.headers["WWW-Authenticate"] == "Bearer"
    assert verify_unchanged(environment.database, before).passed


def test_stale_version_leaves_committed_assignment_unchanged(client, environment):
    before = snapshot(environment.database)
    auth = headers(environment)
    assert client.put(ASSIGNMENT, json=PAYLOAD, headers=auth).status_code == 200
    assert verify_inc001(environment, before).passed

    committed = snapshot(environment.database)
    stale = client.put(
        ASSIGNMENT, json={**PAYLOAD, "assignee_id": "tech-a"}, headers=auth
    )
    assert stale.status_code == 409
    assert verify_unchanged(environment.database, committed).passed


def test_concurrent_assignments_with_same_version_commit_once(client, environment):
    before = snapshot(environment.database)
    auth = headers(environment)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda _: client.put(ASSIGNMENT, json=PAYLOAD, headers=auth), range(2)
            )
        )
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert verify_inc001(environment, before).passed


def test_audit_insert_failure_rolls_back_ticket_update(client, environment):
    before = snapshot(environment.database)
    with closing(sqlite3.connect(environment.database)) as db, db:
        db.execute(
            """
            CREATE TRIGGER fail_audit BEFORE INSERT ON ticket_events
            WHEN EXISTS (
                SELECT 1 FROM tickets
                WHERE id = NEW.ticket_id
                  AND version = NEW.ticket_version
                  AND assignee_id = NEW.assignee_id
            )
            BEGIN
                SELECT RAISE(ABORT, 'audit insert failed after ticket update');
            END
            """
        )

    with pytest.raises(
        sqlite3.IntegrityError, match="audit insert failed after ticket update"
    ):
        client.put(ASSIGNMENT, json=PAYLOAD, headers=headers(environment))
    assert verify_unchanged(environment.database, before).passed


def test_environments_are_isolated_and_recreate_the_same_fixture(client, environment):
    before = snapshot(environment.database)
    with isolated_environment() as second:
        assert second.database != environment.database
        assert set(second.tokens.values()).isdisjoint(environment.tokens.values())
        assert snapshot(second.database) == before
        with TestClient(create_app(second)) as second_client:
            assert (
                second_client.get(ASSIGNMENT, headers=headers(environment)).status_code
                == 401
            )
        assert (
            client.put(
                ASSIGNMENT, json=PAYLOAD, headers=headers(environment)
            ).status_code
            == 200
        )
        assert verify_inc001(environment, before).passed
        assert verify_unchanged(second.database, before).passed
    assert not second.database.exists()

    with isolated_environment() as recreated:
        assert snapshot(recreated.database) == before


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("UPDATE tickets SET assignee_id = 'tech-a' WHERE id = 'INC-001'", "Ticket"),
        ("UPDATE tickets SET version = 3 WHERE id = 'INC-001'", "Ticket"),
        ("UPDATE tickets SET assignee_id = NULL WHERE id = 'INC-002'", "Ticket"),
        ("UPDATE users SET role = 'dispatcher' WHERE id = 'tech-a'", "User"),
        ("UPDATE ticket_events SET actor_id = 'tech-a'", "Audit"),
        ("DELETE FROM ticket_events", "Audit"),
        (
            "INSERT INTO ticket_events SELECT ticket_id, ticket_version + 1, "
            "event_type, actor_id, previous_assignee_id, assignee_id "
            "FROM ticket_events",
            "Audit",
        ),
    ],
)
def test_oracle_detects_corrupt_persisted_state(client, environment, mutation, reason):
    before = snapshot(environment.database)
    assert (
        client.put(ASSIGNMENT, json=PAYLOAD, headers=headers(environment)).status_code
        == 200
    )
    assert verify_inc001(environment, before).passed
    with closing(sqlite3.connect(environment.database)) as db, db:
        db.execute(mutation)
    assert any(
        mismatch.startswith(reason)
        for mismatch in verify_inc001(environment, before).mismatches
    )
