"""Run a fresh SDK agent against a local HTTP ITSM fixture for each execution."""

import asyncio
import re
import sqlite3
from collections.abc import Callable

import httpx
from agents import Agent, Model, ModelSettings, function_tool
from agents.tool_context import ToolContext

from agentchaos.adapters.openai_agents import OpenAIAgentsAdapter
from agentchaos.models import HTTP_503_BEFORE_COMMIT, AgentRun, FaultSpec
from agentchaos.tracing import TraceRecorder

from .database import isolated_environment
from .evidence import ACTOR_ID, TICKET_ID, capture_evidence
from .loopback import serve_fixture
from .oracle import snapshot
from .service_fault import TOOL_CALL_HEADER, AssignmentMonitor

_INSTRUCTIONS = (
    "You are an authorized ITSM dispatcher. Look up the requested ticket with "
    "get_ticket, then use get_assignment to get its current version. Assign the "
    "requested technician with assign_ticket using that version. If a temporary "
    "tool failure occurs, an assignment returns HTTP 503, or the output is malformed, "
    "read the current assignment "
    "before retrying once. If the assignment already matches, verify the state "
    "and do not write again. On HTTP 409, read the latest assignment before any "
    "retry. Verify the final assignment with get_assignment and report what you "
    "observed. Report failures honestly."
)


def _http_agent(
    client: httpx.AsyncClient, model: Model | str | None
) -> OpenAIAgentsAdapter:
    async def request(
        method: str,
        ticket_id: str,
        suffix: str = "",
        *,
        payload: dict[str, str | int] | None = None,
        tool_call_id: str | None = None,
    ) -> str:
        if re.fullmatch(r"INC-[0-9]{3}", ticket_id) is None:
            raise ValueError("Invalid ticket ID. Use the INC-000 format.")
        try:
            response = await client.request(
                method,
                f"/tickets/{ticket_id}{suffix}",
                json=payload,
                headers={TOOL_CALL_HEADER: tool_call_id} if tool_call_id else None,
            )
        except httpx.RequestError:
            raise RuntimeError(
                "ITSM service request failed. Check the assignment."
            ) from None
        # The service's 503 is an HTTP outcome carried in a normal SDK tool return.
        if method == "PUT" and response.status_code == 503:
            return response.text
        if response.status_code != 200:
            raise RuntimeError(f"ITSM service returned HTTP {response.status_code}.")
        return response.text

    @function_tool
    async def get_ticket(ticket_id: str) -> str:
        """Retrieve a ticket by its incident ID."""
        return await request("GET", ticket_id)

    @function_tool
    async def get_assignment(ticket_id: str) -> str:
        """Retrieve the current assignee and version of a ticket."""
        return await request("GET", ticket_id, "/assignment")

    @function_tool
    async def assign_ticket(
        context: ToolContext[None],
        ticket_id: str,
        assignee_id: str,
        expected_version: int,
    ) -> str:
        """Assign a technician using the version last read for the ticket."""
        return await request(
            "PUT",
            ticket_id,
            "/assignment",
            payload={"assignee_id": assignee_id, "expected_version": expected_version},
            tool_call_id=context.tool_call_id,
        )

    return OpenAIAgentsAdapter(
        Agent(
            name="ITSM dispatcher",
            instructions=_INSTRUCTIONS,
            model=model,
            model_settings=ModelSettings(parallel_tool_calls=False),
            tools=[get_ticket, get_assignment, assign_ticket],
        ),
        record_tool_turns=True,
    )


class ITSMAssignmentAdapter:
    """Scope the SDK agent, client, listener, and database to one run."""

    def __init__(self, model_factory: Callable[[], Model | str | None]) -> None:
        self._model_factory = model_factory

    async def run(
        self, task: str, *, recorder: TraceRecorder, fault: FaultSpec | None = None
    ) -> AgentRun:
        fault_occurrence = None
        if fault is not None and fault.fault_type == HTTP_503_BEFORE_COMMIT:
            fault_occurrence = fault.occurrence
            fault = None
        monitor = AssignmentMonitor(
            TICKET_ID, run_id=recorder.run_id, fault_occurrence=fault_occurrence
        )
        with isolated_environment() as environment:
            before = snapshot(environment.database)
            run = None
            try:
                async with serve_fixture(environment, monitor=monitor) as base_url:
                    async with httpx.AsyncClient(
                        base_url=base_url,
                        headers={
                            "Authorization": f"Bearer {environment.tokens[ACTOR_ID]}"
                        },
                        trust_env=False,
                        timeout=5,
                    ) as client:
                        adapter = _http_agent(client, self._model_factory())
                        run = await adapter.run(task, recorder=recorder, fault=fault)
            except (Exception, asyncio.CancelledError) as exc:
                observed = getattr(exc, "agentchaos_run", run)
                if isinstance(observed, AgentRun):
                    try:
                        exc.agentchaos_run = capture_evidence(
                            observed, environment.database, before, monitor
                        )
                    except (sqlite3.Error, ValueError) as evidence_error:
                        exc.add_note(
                            "ITSM oracle evidence unavailable "
                            f"({type(evidence_error).__name__})"
                        )
                raise
            assert run is not None
            return capture_evidence(run, environment.database, before, monitor)
