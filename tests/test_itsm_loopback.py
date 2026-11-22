"""ITSM end-to-end test over a real TCP loopback listener."""

import asyncio
import socket

import httpx
import pytest
import uvicorn

from agentchaos.benchmarks.itsm.api import create_app
from agentchaos.benchmarks.itsm.database import isolated_environment
from agentchaos.benchmarks.itsm.oracle import (
    snapshot,
    verify_assignment,
    verify_unchanged,
)


@pytest.mark.asyncio
async def test_assignment_over_loopback_http():
    with isolated_environment() as environment, socket.socket() as listener:
        before = snapshot(environment.database)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        config = uvicorn.Config(
            create_app(environment),
            host="127.0.0.1",
            port=port,
            proxy_headers=False,
            log_level="warning",
            log_config=None,
            timeout_graceful_shutdown=2,
        )
        server = uvicorn.Server(config)
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if serving.done():
                        serving.result()
                        pytest.fail("Uvicorn exited before the listener was ready")
                    await asyncio.sleep(0.01)
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5
            ) as client:
                url = "/tickets/INC-002/assignment"
                payload = {"assignee_id": "tech-b", "expected_version": 1}
                tech = {"Authorization": f"Bearer {environment.tokens['tech-a']}"}
                dispatcher = {
                    "Authorization": f"Bearer {environment.tokens['dispatcher']}"
                }
                denied = await client.put(url, headers=tech, json=payload)
                assert denied.status_code == 403
                assert verify_unchanged(environment.database, before).passed

                assigned = await client.put(url, headers=dispatcher, json=payload)
                assert assigned.status_code == 200
                assert assigned.json() == {
                    "ticket_id": "INC-002",
                    "assignee_id": "tech-b",
                    "version": 2,
                }
                current = await client.get(url, headers=tech)
                assert current.status_code == 200
                assert current.json() == assigned.json()
                audit = await client.get("/tickets/INC-002/audit", headers=tech)
                assert audit.status_code == 200
                assert audit.json() == [
                    {
                        "ticket_id": "INC-002",
                        "ticket_version": 2,
                        "event_type": "assigned",
                        "actor_id": "dispatcher",
                        "previous_assignee_id": "tech-a",
                        "assignee_id": "tech-b",
                    }
                ]
                result = verify_assignment(
                    environment.database,
                    before,
                    ticket_id="INC-002",
                    actor_id="dispatcher",
                    assignee_id="tech-b",
                )
                assert result.passed, result.mismatches
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=5)
