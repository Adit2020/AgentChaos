"""Run the actual Agents SDK against real TCP and SQLite with an offline model."""

import asyncio
import json
import socket
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

import pytest
from agents import Model, ModelResponse, Usage
from httpx import ASGITransport, AsyncClient
from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage
from openai.types.responses.response_output_text import ResponseOutputText

from agentchaos import (
    EventKind,
    Experiment,
    ExperimentRunner,
    FaultSpec,
    MultiRunRunner,
    RunStatus,
    compare_runs,
)
from agentchaos.api import create_app
from agentchaos.artifacts import (
    export_experiment,
    export_multi_run,
    load_export,
    load_multi_run_export,
)
from agentchaos.benchmarks.itsm import agent
from agentchaos.benchmarks.itsm.database import Environment, isolated_environment
from agentchaos.benchmarks.itsm.evidence import (
    ASSIGNEE_ID,
    ASSIGNMENT_TOOL,
    DEFAULT_TASK,
    TICKET_ID,
    evaluate_assignment,
    read_evidence,
    read_service_evidence,
)
from agentchaos.benchmarks.itsm.oracle import snapshot
from agentchaos.benchmarks.itsm.service_fault import UNAVAILABLE_CODE, AssignmentMonitor
from agentchaos.models import HTTP_503_BEFORE_COMMIT
from agentchaos.scenarios import configured_fault, itsm_assignment

CLAIM = "Assignment succeeded."


def call(name: str, call_id: str, **arguments: Any) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        name=name,
        call_id=call_id,
        arguments=json.dumps({"ticket_id": TICKET_ID, **arguments}),
        type="function_call",
    )


def assign(
    call_id: str,
    version: int = 1,
    *,
    ticket: str = TICKET_ID,
    assignee: str = ASSIGNEE_ID,
) -> ResponseFunctionToolCall:
    return call(
        ASSIGNMENT_TOOL,
        call_id,
        ticket_id=ticket,
        assignee_id=assignee,
        expected_version=version,
    )


class Script(Model):
    def __init__(
        self, *turns: ResponseFunctionToolCall | str | BaseException | asyncio.Event
    ) -> None:
        self.turns = iter(turns)
        self.inputs: list[list[Any]] = []

    async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        self.inputs.append(kwargs["input"])
        turn = next(self.turns)
        if isinstance(turn, BaseException):
            raise turn
        if isinstance(turn, asyncio.Event):
            turn.set()
            await asyncio.Future()
            raise AssertionError("Test should cancel the model while paused")
        output = turn
        if isinstance(turn, str):
            output = ResponseOutputMessage(
                id=f"answer-{len(self.inputs)}",
                content=[
                    ResponseOutputText(annotations=[], text=turn, type="output_text")
                ],
                role="assistant",
                status="completed",
                type="message",
            )
        return ModelResponse(output=[output], usage=Usage(), response_id=None)

    def stream_response(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Scripted benchmark models do not stream")

    def tool_output(self, call_id: str) -> str:
        for turn in reversed(self.inputs):
            for item in turn:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "function_call_output"
                    and item["call_id"] == call_id
                ):
                    return item["output"]
        raise AssertionError(f"Missing tool output for {call_id}")


def baseline_script() -> Script:
    return Script(
        call("get_ticket", "ticket"),
        call("get_assignment", "initial"),
        assign("write"),
        call("get_assignment", "verified"),
        CLAIM,
    )


def faulted_script(*, retry: bool) -> Script:
    recovery = [assign("retry"), call("get_assignment", "verified")] if retry else []
    return Script(
        call("get_assignment", "initial"),
        assign("injected"),
        call("get_assignment", "after-injection"),
        *recovery,
        CLAIM,
    )


def experiment(fault_type="tool_error", repetition=0, *, occurrence=1) -> Experiment:
    return Experiment(
        experiment_id=f"itsm-{repetition}",
        baseline_run_id=f"itsm-baseline-{repetition}",
        task=DEFAULT_TASK,
        fault=(
            FaultSpec(fault_type=HTTP_503_BEFORE_COMMIT, occurrence=occurrence)
            if fault_type == HTTP_503_BEFORE_COMMIT
            else configured_fault(fault_type, ASSIGNMENT_TOOL, occurrence)
        ),
    )


@pytest.fixture
def fixture_resources(monkeypatch):
    """Observe resource identities while still using their real implementations."""
    resources: list[tuple[Environment, str]] = []
    listeners = agent.serve_fixture
    clients: list[AsyncClient] = []
    build_agent = agent._http_agent

    @asynccontextmanager
    async def observe_listener(env, *, monitor):
        async with listeners(env, monitor=monitor) as url:
            resources.append((env, url))
            yield url

    def observe_client(client, model):
        clients.append(client)
        return build_agent(client, model)

    monkeypatch.setattr(agent, "serve_fixture", observe_listener)
    monkeypatch.setattr(agent, "_http_agent", observe_client)
    yield resources
    assert len(clients) == len(resources)
    assert all(client.is_closed for client in clients)
    for env, url in resources:
        assert not env.database.parent.exists()
        with socket.socket() as probe:
            probe.settimeout(0.5)
            assert probe.connect_ex(("127.0.0.1", urlsplit(url).port)) != 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault_type", "retry"),
    [
        ("tool_error", False),
        ("tool_error", True),
        ("tool_timeout", True),
        ("malformed_tool_output", True),
    ],
)
async def test_real_sdk_isolated_http_oracle_and_artifacts(
    fault_type, retry, tmp_path, fixture_resources
):
    baseline_model = baseline_script()
    faulted_model = faulted_script(retry=retry)
    models = iter((baseline_model, faulted_model))
    outcome = await ExperimentRunner().run(
        adapter=agent.ITSMAssignmentAdapter(models.__next__),
        experiment=experiment(fault_type),
        evaluator=evaluate_assignment,
    )

    assert len(fixture_resources) == 2
    first, second = (env for env, _ in fixture_resources)
    assert first.database != second.database
    assert first.tokens["dispatcher"] != second.tokens["dispatcher"]

    baseline = read_evidence(outcome.baseline)
    faulted = read_evidence(outcome.faulted)
    assert baseline and faulted
    assert baseline.run_id == outcome.baseline.run_id
    assert faulted.run_id == outcome.faulted.run_id
    assert baseline.before == faulted.before
    assert baseline.before and baseline.before.assignee_id is None
    assert baseline.before.version == 1
    assert baseline.passed
    assert baseline.after and baseline.after.assignee_id == ASSIGNEE_ID
    assert baseline.after.version == 2
    assert len(baseline.audit) == 1
    audit = baseline.audit[0]
    assert audit.actor_id == "dispatcher"
    assert audit.previous_assignee_id is None
    assert audit.assignee_id == ASSIGNEE_ID
    assert audit.ticket_version == 2

    assert json.loads(baseline_model.tool_output("ticket"))["id"] == TICKET_ID
    assert json.loads(baseline_model.tool_output("verified"))["version"] == 2
    initial = json.loads(faulted_model.tool_output("initial"))
    after_injection = json.loads(faulted_model.tool_output("after-injection"))
    assert (
        initial
        == after_injection
        == {"ticket_id": TICKET_ID, "assignee_id": None, "version": 1}
    )
    assert faulted.passed is retry
    assert faulted.audit == (baseline.audit if retry else ())
    assert faulted.after == (baseline.after if retry else faulted.before)
    assert outcome.faulted.output == CLAIM
    assert outcome.faulted.status is RunStatus.SUCCEEDED

    comparison = outcome.comparison
    assert comparison.baseline_task_success is True
    assert comparison.fault_triggered is True
    assert comparison.faulted_injected_failures == 1
    assert comparison.target_tool_retries == int(retry)
    assert comparison.faulted_task_success is retry
    assert comparison.recovered is retry
    service = read_service_evidence(outcome.faulted)
    assert service is not None
    assert service.armed_occurrence is None
    assert service.injected_response is None
    assert service.requests_received == int(retry)

    directory = export_experiment(outcome, root=tmp_path)
    restored = load_export(directory.name, root=tmp_path)
    assert read_evidence(restored.baseline) == baseline
    assert read_evidence(restored.faulted) == faulted
    assert evaluate_assignment(restored.faulted) is retry
    assert restored.comparison == comparison
    saved = "\n".join(path.read_text() for path in directory.glob("*.json"))
    model_contexts = repr((baseline_model.inputs, faulted_model.inputs))
    for env in (first, second):
        assert all(
            token not in saved and token not in model_contexts
            for token in env.tokens.values()
        )
        assert str(env.database) not in saved


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancel", "fault_type", "commit"),
    [
        (False, "tool_error", True),
        (True, "tool_error", True),
        (False, HTTP_503_BEFORE_COMMIT, True),
        (True, HTTP_503_BEFORE_COMMIT, True),
        (True, HTTP_503_BEFORE_COMMIT, False),
    ],
)
async def test_commit_survives_failed_or_cancelled_sdk_run(
    cancel, fault_type, commit, tmp_path, fixture_resources
):
    pause = asyncio.Event()
    stop = pause if cancel else RuntimeError("script failed after commit")
    retries = [assign("retry")] if commit else []
    models = iter((baseline_script(), Script(assign("injected"), *retries, stop)))
    run = asyncio.create_task(
        ExperimentRunner().run(
            adapter=agent.ITSMAssignmentAdapter(models.__next__),
            experiment=experiment(fault_type),
            evaluator=evaluate_assignment,
        )
    )

    try:
        if cancel:
            async with asyncio.timeout(5):
                await pause.wait()
            run.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError) as error:
            await run
    finally:
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)

    assert len(fixture_resources) == 2
    partial = error.value.agentchaos_experiment
    trace = error.value.agentchaos_run
    assert trace == partial.faulted
    assert trace.status is (RunStatus.CANCELLED if cancel else RunStatus.FAILED)
    assert trace.events[-1].kind is (
        EventKind.RUN_CANCELLED if cancel else EventKind.RUN_FAILED
    )
    assert trace.output is None
    evidence = read_evidence(trace)
    assert evidence and evidence.passed is commit
    assert len(evidence.audit) == int(commit)
    assert partial.comparison.faulted_task_success is False
    assert partial.comparison.recovered is (
        None if fault_type == HTTP_503_BEFORE_COMMIT else False
    )
    directory = export_experiment(partial, root=tmp_path)
    restored = load_export(directory.name, root=tmp_path).faulted
    assert read_evidence(restored) == evidence
    if fault_type == HTTP_503_BEFORE_COMMIT:
        service = read_service_evidence(restored)
        assert service == read_service_evidence(trace)
        assert partial.comparison.fault_triggered is True
        assert service and service.injected_response
        assert (service.first_commit_after_trigger is not None) is commit


@pytest.mark.asyncio
async def test_registered_itsm_job_uses_server_owned_configuration(
    tmp_path, monkeypatch
):
    builds = 0

    def offline_factory():
        nonlocal builds
        builds += 1
        models = iter((baseline_script(), faulted_script(retry=True)))
        return agent.ITSMAssignmentAdapter(models.__next__)

    monkeypatch.setattr(itsm_assignment, "create_adapter", offline_factory)
    app = create_app(tmp_path)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client,
    ):
        listing = await client.get("/api/scenarios")
        registered = {row["scenario_id"]: row for row in listing.json()["scenarios"]}
        assert registered["itsm_assignment"]["default_task"] == DEFAULT_TASK
        assert registered["itsm_assignment"]["supported_target_tools"] == [
            ASSIGNMENT_TOOL
        ]
        request = {
            "scenario_id": "itsm_assignment",
            "fault_type": "tool_error",
            "target_tool": ASSIGNMENT_TOOL,
            "occurrence": 1,
            "acknowledge_live_cost": False,
        }
        assert (await client.post("/api/jobs", json=request)).status_code == 422
        assert builds == 0

        request["acknowledge_live_cost"] = True
        arbitrary = await client.post(
            "/api/jobs", json={**request, "url": "http://example.com"}
        )
        assert arbitrary.status_code == 422
        service_fault = await client.post(
            "/api/jobs", json={**request, "fault_type": HTTP_503_BEFORE_COMMIT}
        )
        assert service_fault.status_code == 422
        assert builds == 0
        submission = await client.post("/api/jobs", json=request)
        assert submission.status_code == 202
        job_id = submission.json()["job_id"]
        async with asyncio.timeout(8):
            while True:
                status = (await client.get(f"/api/jobs/{job_id}")).json()
                if status["finished_at"] is not None:
                    break
                await asyncio.sleep(0.01)
        assert builds == 1
        assert status["state"] == "completed"
        artifact = load_export(status["export_id"], root=tmp_path)
        assert evaluate_assignment(artifact.baseline) is True
        assert evaluate_assignment(artifact.faulted) is True
        assert artifact.comparison.recovered is True


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [False, True])
async def test_service_503_preserves_independent_state_and_artifacts(
    retry, tmp_path, fixture_resources
):
    with isolated_environment() as clean:
        initial_db = snapshot(clean.database)

    class ObserveAfter503(Script):
        async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
            if len(self.inputs) == 3:
                # The 503 is already in model input. No retry has been issued.
                env, _ = fixture_resources[-1]
                assert snapshot(env.database) == initial_db
            return await super().get_response(*args, **kwargs)

    def build_faulted_script():
        recovery = [assign("retry")] if retry else []
        return ObserveAfter503(
            call("get_ticket", "ticket"),
            call("get_assignment", "before"),
            assign("unavailable"),
            call("get_assignment", "after-503"),
            *recovery,
            CLAIM,
        )

    # Reuse one adapter for two pairs. Arming and request counts must reset.
    scripts = [(baseline_script(), build_faulted_script()) for _ in range(2)]
    models = iter(model for pair in scripts for model in pair)
    adapter = agent.ITSMAssignmentAdapter(models.__next__)
    request_ids = set()
    for repetition, (baseline_model, faulted_model) in enumerate(scripts):
        outcome = await ExperimentRunner().run(
            adapter=adapter,
            experiment=experiment(HTTP_503_BEFORE_COMMIT, repetition),
            evaluator=evaluate_assignment,
        )
        directory = export_experiment(outcome, root=tmp_path)
        restored = load_export(directory.name, root=tmp_path)
        assert restored.manifest.experiment.fault == outcome.experiment.fault
        assert restored.comparison == outcome.comparison
        service = read_service_evidence(restored.faulted)
        baseline_service = read_service_evidence(restored.baseline)
        baseline = read_evidence(restored.baseline)
        faulted = read_evidence(restored.faulted)
        assert service and baseline_service and baseline and faulted
        assert service == read_service_evidence(outcome.faulted)
        assert faulted == read_evidence(outcome.faulted)

        assert baseline_service.armed_occurrence is None
        assert baseline_service.injected_response is None
        assert baseline_service.requests_received == 1
        assert (
            baseline_service.first_commit
            and baseline_service.first_commit.status_code == 200
        )
        assert baseline.passed and baseline.after and baseline.after.version == 2
        assert len(baseline.audit) == 1

        assert service.run_id == restored.faulted.run_id
        assert service.ticket_id == TICKET_ID
        assert service.operation == "PUT /tickets/{ticket_id}/assignment"
        assert service.armed_occurrence == 1
        assert service.requests_received == (2 if retry else 1)
        rejection = service.injected_response
        assert rejection and rejection.occurrence == 1
        assert rejection.status_code == 503 and rejection.committed is False
        assert rejection.request_id not in request_ids
        request_ids.add(rejection.request_id)

        assert json.loads(faulted_model.tool_output("unavailable")) == {
            "detail": {
                "status": 503,
                "code": UNAVAILABLE_CODE,
                "request_id": rejection.request_id,
            }
        }
        assert json.loads(faulted_model.tool_output("ticket"))["id"] == TICKET_ID
        before = json.loads(faulted_model.tool_output("before"))
        assert before == json.loads(faulted_model.tool_output("after-503"))
        assert before == {"ticket_id": TICKET_ID, "assignee_id": None, "version": 1}

        event = next(
            event
            for event in restored.faulted.events
            if event.call_id == "unavailable" and event.kind is EventKind.TOOL_RESULT
        )
        assert event.attributes["success"] is True
        assert not event.attributes.get("injected")
        assert restored.faulted.output == CLAIM
        assert restored.comparison.faulted_injected_failures == 0
        assert restored.comparison.fault_triggered is True
        assert restored.comparison.recovered is retry
        assert restored.comparison.baseline_task_success is True
        assert restored.comparison.faulted_task_success is retry
        assert faulted.passed is retry
        assert faulted.before == baseline.before
        assert faulted.after == (baseline.after if retry else faulted.before)
        assert faulted.audit == (baseline.audit if retry else ())

        if retry:
            commit = service.first_commit_after_trigger
            assert commit and commit == service.first_commit
            assert commit.occurrence == 2 and commit.request_id != rejection.request_id
            assert commit.committed is True and commit.status_code == 200
            assert commit.resulting_version == 2
            assert commit.intent == rejection.intent
            assert commit.after_503_sent is True
            assert json.loads(faulted_model.tool_output("retry"))["version"] == 2
        else:
            assert service.first_commit is service.first_commit_after_trigger is None

        saved = "\n".join(file.read_text() for file in directory.glob("*.json"))
        model_inputs = repr((baseline_model.inputs, faulted_model.inputs))
        for env, _ in fixture_resources[-2:]:
            assert all(
                token not in saved and token not in model_inputs
                for token in env.tokens.values()
            )


@pytest.mark.asyncio
async def test_service_503_only_counts_authorized_target_writes_under_concurrency():
    monitor = AssignmentMonitor(TICKET_ID, run_id="direct", fault_occurrence=2)
    assignment_url = f"/tickets/{TICKET_ID}/assignment"
    payload = {"assignee_id": ASSIGNEE_ID, "expected_version": 1}
    with isolated_environment() as env:
        async with (
            agent.serve_fixture(env, monitor=monitor) as url,
            AsyncClient(
                base_url=url,
                trust_env=False,
                headers={"Authorization": f"Bearer {env.tokens['dispatcher']}"},
            ) as client,
        ):
            assert (await client.get(assignment_url)).status_code == 200
            unauthorized = await client.put(
                assignment_url,
                json=payload,
                headers={"Authorization": f"Bearer {env.tokens['tech-a']}"},
            )
            assert unauthorized.status_code == 403
            invalid = await client.put(assignment_url, json={**payload, "fault": 503})
            assert invalid.status_code == 422
            other = await client.put("/tickets/INC-002/assignment", json=payload)
            assert other.status_code == 200
            assert monitor.evidence().requests_received == 0

            first = await client.put(assignment_url, json=payload)
            assert first.status_code == 200
            replies = await asyncio.gather(
                *(
                    client.put(assignment_url, json={**payload, "expected_version": 2})
                    for _ in range(2)
                )
            )
            assert sorted(reply.status_code for reply in replies) == [200, 503]
            fourth = await client.put(
                assignment_url, json={**payload, "expected_version": 3}
            )
            assert fourth.status_code == 200
            state = snapshot(env.database)
            assert [
                event.ticket_version
                for event in state.audit
                if event.ticket_id == TICKET_ID
            ] == [2, 3, 4]

        evidence = monitor.evidence()
        assert evidence.requests_received == 4
        assert evidence.injected_response and evidence.injected_response.occurrence == 2
        rejected = next(reply for reply in replies if reply.status_code == 503)
        assert (
            rejected.json()["detail"]["request_id"]
            == evidence.injected_response.request_id
        )
        assert evidence.first_commit and evidence.first_commit.occurrence == 1
        assert (
            evidence.first_commit_after_trigger
            and evidence.first_commit_after_trigger.occurrence == 3
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requests", "baseline_ok", "triggered", "verdict", "audit_versions"),
    [
        pytest.param(
            [assign("before-trigger")], True, False, None, [2], id="no-trigger"
        ),
        pytest.param([assign("retry", 2)], True, True, False, [], id="stale-retry"),
        pytest.param(
            [assign("different-ticket", ticket="INC-002")],
            True,
            True,
            False,
            [],
            id="unrelated-commit",
        ),
        pytest.param(
            [assign("wrong-assignee", assignee="tech-a")],
            True,
            True,
            False,
            [2],
            id="wrong-assignee",
        ),
        pytest.param(
            [assign("correct"), assign("duplicate", 2)],
            True,
            True,
            False,
            [2, 3],
            id="duplicate-assignment",
        ),
        pytest.param(
            [assign("correct"), assign("extra", ticket="INC-002")],
            True,
            True,
            False,
            [2],
            id="unrelated-mutation-after-correct-assignment",
        ),
        pytest.param([assign("correct")], False, True, None, [2], id="bad-baseline"),
    ],
)
async def test_service_recovery_requires_correct_business_outcome(
    requests, baseline_ok, triggered, verdict, audit_versions
):
    initial = [assign("unavailable")] if triggered else []
    faulted_model = Script(*initial, *requests, CLAIM)
    models = iter((baseline_script() if baseline_ok else Script(CLAIM), faulted_model))
    outcome = await ExperimentRunner().run(
        adapter=agent.ITSMAssignmentAdapter(models.__next__),
        experiment=experiment(HTTP_503_BEFORE_COMMIT, occurrence=1 if triggered else 2),
        # Even an evaluator accepting the model's claim cannot prove a recovery.
        expected_output=CLAIM,
    )
    assert outcome.comparison.fault_triggered is triggered
    assert outcome.comparison.recovered is verdict
    oracle = read_evidence(outcome.faulted)
    assert oracle and [event.ticket_version for event in oracle.audit] == audit_versions
    assert oracle.passed is (not baseline_ok or not triggered)
    service = read_service_evidence(outcome.faulted)
    assert service is not None
    if requests and requests[0].call_id == "retry":
        terminal = next(
            event
            for event in outcome.faulted.events
            if event.call_id == "retry" and event.kind is EventKind.TOOL_RESULT
        )
        assert terminal.attributes["success"] is False
        assert service.requests_received == 2
    if requests and requests[0].call_id == "different-ticket":
        assert json.loads(faulted_model.tool_output("different-ticket"))["version"] == 2
        assert service.requests_received == 1


@pytest.mark.asyncio
async def test_service_recovery_rejects_uncorrelated_or_legacy_records(tmp_path):
    models = iter((baseline_script(), faulted_script(retry=True)))
    outcome = await ExperimentRunner().run(
        adapter=agent.ITSMAssignmentAdapter(models.__next__),
        experiment=experiment(HTTP_503_BEFORE_COMMIT),
        evaluator=evaluate_assignment,
    )
    assert outcome.comparison.recovered is True

    def replace_field(data, path, value):
        for key in path[:-1]:
            data = data[key]
        data[path[-1]] = value

    retries = ("first_commit", "first_commit_after_trigger")
    evidence_key = "itsm_assignment_http"
    for path, value in (
        (("run_id",), outcome.baseline.run_id),
        (("injected_response", "run_id"), outcome.baseline.run_id),
        (("ticket_id",), "INC-002"),
        (("operation",), "GET /tickets/{ticket_id}/assignment"),
        (("armed_occurrence",), 2),
        (("requests_received",), 1),
        (("response_sent",), None),
        (("injected_response", "intent"), None),
        (("injected_response", "intent", "actor_id"), "tech-a"),
        (("injected_response", "intent", "assignee_id"), "tech-a"),
        (("first_commit_after_trigger",), None),
        ((retries, "occurrence"), 1),
        ((retries, "run_id"), outcome.baseline.run_id),
        ((retries, "after_503_sent"), False),
        ((retries, "tool_call_id"), None),
        ((retries, "tool_call_id"), "injected"),
        ((retries, "intent", "expected_version"), 2),
        ((retries, "resulting_version"), 3),
        (
            (retries, "request_id"),
            read_service_evidence(outcome.faulted).injected_response.request_id,
        ),
    ):
        faulted = outcome.faulted.model_copy(deep=True)
        data = faulted.events[-1].attributes[evidence_key]
        if isinstance(path[0], tuple):
            for key in path[0]:
                replace_field(data, (key, *path[1:]), value)
        else:
            replace_field(data, path, value)
        comparison = compare_runs(outcome.experiment, outcome.baseline, faulted)
        assert comparison.recovered is None, path

    faulted = outcome.faulted.model_copy(deep=True)
    faulted.events[-1].attributes["itsm_oracle"] = outcome.baseline.events[
        -1
    ].attributes["itsm_oracle"]
    assert compare_runs(outcome.experiment, outcome.baseline, faulted).recovered is None

    retry_turn = next(
        event
        for event in outcome.faulted.events
        if event.kind == EventKind.MODEL_RESPONSE
        and event.attributes["output_tool_call_ids"] == ["retry"]
    )
    model_request = next(
        event
        for event in outcome.faulted.events
        if event.kind == EventKind.MODEL_REQUEST and event.call_id == retry_turn.call_id
    )
    for outputs in ([], [{"call_id": "injected", "sha256": "wrong"}]):
        faulted = outcome.faulted.model_copy(deep=True)
        faulted.events[model_request.sequence].attributes["input_tool_outputs"] = (
            outputs
        )
        assert (
            compare_runs(outcome.experiment, outcome.baseline, faulted).recovered
            is None
        )

    faulted = outcome.faulted.model_copy(deep=True)
    faulted.events[
        retry_turn.sequence
    ].event_id = f"{outcome.baseline.run_id}:{retry_turn.sequence}"
    assert compare_runs(outcome.experiment, outcome.baseline, faulted).recovered is None

    faulted = outcome.faulted.model_copy(deep=True)
    legacy = faulted.events[-1].attributes[evidence_key]
    del legacy["response_sent"]
    for key in ("injected_response", *retries):
        del legacy[key]["intent"]
        del legacy[key]["after_503_sent"]
        del legacy[key]["run_id"]
        del legacy[key]["tool_call_id"]
    assert read_service_evidence(faulted) is not None
    result = compare_runs(
        outcome.experiment, outcome.baseline, faulted, evaluator=evaluate_assignment
    )
    old = replace(outcome, faulted=faulted, comparison=result)
    directory = export_experiment(old, root=tmp_path)
    restored = load_export(directory.name, root=tmp_path)
    assert restored.comparison.recovered is None
    assert read_service_evidence(restored.faulted) == read_service_evidence(faulted)


@pytest.mark.asyncio
async def test_service_recovery_multirun_denominator_and_round_trip(tmp_path):
    models = iter(
        (
            baseline_script(),
            faulted_script(retry=True),
            baseline_script(),
            faulted_script(retry=False),
            baseline_script(),
            Script(CLAIM),
        )
    )
    template = experiment(HTTP_503_BEFORE_COMMIT)
    report = await MultiRunRunner(max_repetitions=3).run(
        adapter=agent.ITSMAssignmentAdapter(models.__next__),
        experiment=template,
        evaluator=evaluate_assignment,
        repetitions=3,
    )
    assert report.triggered_faults == 2
    assert report.confirmed_recoveries == report.confirmed_nonrecoveries == 1
    assert report.unknown_recoveries == 1
    assert report.evaluable_triggered_repetitions == 2
    assert report.confirmed_recovery_rate == 0.5
    assert [outcome.comparison.recovered for outcome in report.outcomes] == [
        True,
        False,
        None,
    ]
    evidence = [read_service_evidence(outcome.faulted) for outcome in report.outcomes]
    assert len({row.run_id for row in evidence}) == 3
    assert (
        evidence[0].injected_response.request_id
        != evidence[1].injected_response.request_id
    )
    for outcome in report.outcomes:
        assert read_evidence(outcome.faulted).run_id == outcome.faulted.run_id

    directory = export_multi_run(report, experiment=template, root=tmp_path)
    restored = load_multi_run_export(directory.name, root=tmp_path)
    restored_outcomes = [rep.outcome for rep in restored.repetitions]
    assert all(restored_outcomes)
    assert [item.comparison.recovered for item in restored_outcomes] == [
        True,
        False,
        None,
    ]
    assert [
        read_service_evidence(item.faulted) for item in restored_outcomes
    ] == evidence


@pytest.mark.asyncio
async def test_same_sdk_turn_can_commit_after_503_without_reactive_retry(
    monkeypatch, tmp_path
):
    order: list[str] = []
    second_started = asyncio.Event()
    sent = asyncio.Event()
    received = asyncio.Event()
    committed = asyncio.Event()

    class ParallelScript(Script):
        async def get_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
            if not self.inputs:
                self.inputs.append(kwargs["input"])
                order.append("model issued both calls")
                return ModelResponse(
                    output=[assign("rejected"), assign("preplanned")],
                    usage=Usage(),
                    response_id=None,
                )
            assert (
                json.loads(self._received(kwargs["input"], "rejected"))["detail"][
                    "status"
                ]
                == 503
            )
            assert (
                json.loads(self._received(kwargs["input"], "preplanned"))["version"]
                == 2
            )
            order.append("model continued with both results")
            return await super().get_response(*args, **kwargs)

        @staticmethod
        def _received(items, call_id):
            return next(
                item["output"]
                for item in items
                if isinstance(item, dict)
                and item.get("type") == "function_call_output"
                and item["call_id"] == call_id
            )

    faulted_model = ParallelScript(CLAIM)
    build = agent._http_agent
    mark_sent = AssignmentMonitor.mark_response_sent
    receive = AssignmentMonitor.receive
    record_commit = AssignmentMonitor.record_commit

    async def observe_sent(monitor):
        await mark_sent(monitor)
        order.append("service sent 503")
        sent.set()

    def observe_arrival(monitor, *args, **kwargs):
        request = receive(monitor, *args, **kwargs)
        if "model issued both calls" in order:
            order.append(f"service received {request.occurrence}")
        return request

    def observe_commit(monitor, request, version):
        record_commit(monitor, request, version)
        if "model issued both calls" in order:
            order.append("service committed")

    def instrument_client(client, model):
        if model is faulted_model:
            started = 0

            async def before_request(request):
                nonlocal started
                started += 1
                ordinal = started
                order.append(f"client invoked {ordinal}")
                if ordinal == 1:
                    await second_started.wait()
                else:
                    second_started.set()
                    await sent.wait()
                    await received.wait()

            async def after_response(response):
                if response.status_code == 503:
                    await sent.wait()
                    await response.aread()
                    order.append("client read 503")
                    received.set()
                    # Keep the rejected tool pending until the other call commits.
                    await committed.wait()
                else:
                    order.append("client received commit response")
                    committed.set()

            client.event_hooks["request"].append(before_request)
            client.event_hooks["response"].append(after_response)
        return build(client, model)

    monkeypatch.setattr(agent, "_http_agent", instrument_client)
    monkeypatch.setattr(AssignmentMonitor, "mark_response_sent", observe_sent)
    monkeypatch.setattr(AssignmentMonitor, "receive", observe_arrival)
    monkeypatch.setattr(AssignmentMonitor, "record_commit", observe_commit)
    models = iter((baseline_script(), faulted_model))
    async with asyncio.timeout(10):
        outcome = await ExperimentRunner().run(
            adapter=agent.ITSMAssignmentAdapter(models.__next__),
            experiment=experiment(HTTP_503_BEFORE_COMMIT),
            evaluator=evaluate_assignment,
        )

    assert order[:4] == [
        "model issued both calls",
        "client invoked 1",
        "client invoked 2",
        "service received 1",
    ]
    assert order[4:6] == ["service sent 503", "client read 503"]
    assert order[6:] == [
        "service received 2",
        "service committed",
        "client received commit response",
        "model continued with both results",
    ]
    evidence = read_service_evidence(outcome.faulted)
    assert evidence and evidence.first_commit_after_trigger
    assert evidence.first_commit_after_trigger.after_503_sent is True
    assert (
        evidence.injected_response
        and evidence.injected_response.tool_call_id == "rejected"
    )
    assert evidence.first_commit_after_trigger.tool_call_id == "preplanned"
    origins = [
        event.attributes["output_tool_call_ids"]
        for event in outcome.faulted.events
        if event.kind == EventKind.MODEL_RESPONSE
    ]
    assert origins == [["rejected", "preplanned"], []]
    assert outcome.comparison.fault_triggered is True
    assert outcome.comparison.faulted_task_success is True
    assert outcome.comparison.recovered is None
    directory = export_experiment(outcome, root=tmp_path)
    restored = load_export(directory.name, root=tmp_path)
    assert restored.comparison.recovered is None
    assert restored.faulted.events == outcome.faulted.events
    assert read_service_evidence(restored.faulted) == evidence
