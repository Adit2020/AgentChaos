"""Offline job API integration tests: real runner/writer, no model requests."""

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from agentchaos import jobs
from agentchaos.adapters.base import FaultCapableAdapter
from agentchaos.api import create_app
from agentchaos.jobs import JobSettings
from agentchaos.models import AgentRun, FaultSpec, RunStatus
from agentchaos.scenarios import Scenario, itsm_assignment, registered_scenarios
from agentchaos.scenarios import order_support as orders
from agentchaos.tracing import TraceRecorder

REQUEST = {
    "scenario_id": "order_support",
    "fault_type": "tool_error",
    "target_tool": "get_order_status_json",
    "occurrence": 1,
    "acknowledge_live_cost": True,
}
SECRET = "sk-test-secret-do-not-disclose /private/secret/config"


class OfflineAdapter:
    def __init__(
        self,
        *,
        gate: asyncio.Event | None = None,
        fail: str | None = None,
        resist_cancellation: bool = False,
    ) -> None:
        self.gate = gate
        self.fail = fail
        self.resist_cancellation = resist_cancellation
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.calls: list[FaultSpec | None] = []

    async def run(
        self, task: str, *, recorder: TraceRecorder, fault: FaultSpec | None = None
    ) -> AgentRun:
        self.calls.append(fault)
        recorder.start(task=task, adapter="offline")
        self.started.set()
        try:
            if self.gate is not None and (
                self.fail != "faulted_timeout" or fault is not None
            ):
                try:
                    await self.gate.wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    if not self.resist_cancellation:
                        raise
                    await self.gate.wait()
            if self.fail == "baseline" or (self.fail == "faulted" and fault):
                raise RuntimeError(SECRET)
            recorder.record_model_request(call_id="m1", name="offline")
            recorder.record_model_response(call_id="m1")
            recorder.record_tool_call(call_id="t1", name="get_order_status_json")
            recorder.record_tool_result(
                call_id="t1",
                attributes=(
                    {
                        "injected": True,
                        "success": False,
                        "fault_kind": fault.fault_type,
                        "target_tool": fault.target_tool,
                        "occurrence": fault.occurrence,
                    }
                    if fault
                    else {"injected": False}
                ),
            )
            if fault:
                recorder.record_model_request(call_id="m2", name="offline")
                recorder.record_model_response(call_id="m2")
                recorder.record_tool_call(call_id="t2", name="get_order_status_json")
                recorder.record_tool_result(
                    call_id="t2", attributes={"injected": False}
                )
            return recorder.finish(
                RunStatus.SUCCEEDED, output="Order 123 is shipped, arriving Friday."
            )
        except (Exception, asyncio.CancelledError) as exc:
            exc.agentchaos_run = recorder.finish(
                RunStatus.CANCELLED
                if isinstance(exc, asyncio.CancelledError)
                else RunStatus.FAILED
            )
            raise


@asynccontextmanager
async def client_for(
    root: Path,
    factory: Callable[[], FaultCapableAdapter] = OfflineAdapter,
    settings: JobSettings | None = None,
) -> AsyncIterator[AsyncClient]:
    default = registered_scenarios()[0]
    scenario = Scenario(default.metadata, factory, default.evaluator)
    app = create_app(root, scenarios=(scenario,), job_settings=settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            yield client


async def finished(client: AsyncClient, job_id: str) -> dict:
    async with asyncio.timeout(3):
        while True:
            result = (await client.get(f"/api/jobs/{job_id}")).json()
            if result["finished_at"] is not None:
                return result
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_discovery_and_startup_never_construct_live_adapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden() -> FaultCapableAdapter:
        pytest.fail("discovery constructed a live adapter")

    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setattr(orders, "create_adapter", forbidden)
    monkeypatch.setattr(itsm_assignment, "create_adapter", forbidden)
    app = create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.get("/api/scenarios")
            assert SECRET not in response.text
            scenarios = {
                item["scenario_id"]: item for item in response.json()["scenarios"]
            }
            assert "itsm_assignment" in scenarios
            scenario = scenarios["order_support"]
            assert scenario["default_task"] == "Where is order 123?"
            assert scenario["supported_target_tools"] == ["get_order_status_json"]
            assert len(scenario["supported_fault_types"]) == 3
            assert "factory" not in response.text
            assert (await client.get("/api/health")).json() == {"status": "ok"}
            assert (await client.get("/api/exports")).json() == {"exports": []}
            paths = (await client.get("/openapi.json")).json()["paths"]
            assert {"/api/scenarios", "/api/jobs", "/api/jobs/{job_id}"} <= paths.keys()


def test_registered_adapter_builds_fresh_sdk_objects_without_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents import Runner

    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("building a scenario called a model")

    monkeypatch.setenv("OPENAI_API_KEY", "sk-offline-placeholder")
    monkeypatch.setattr(Runner, "run", forbidden)
    first = orders.create_adapter()
    second = orders.create_adapter()
    assert first._agent is not second._agent
    assert first._agent.tools[0] is not second._agent.tools[0]
    assert first._agent.tools[0].name == "get_order_status_json"


@pytest.mark.asyncio
async def test_job_transitions_export_and_concurrent_rejection(tmp_path: Path) -> None:
    gate = asyncio.Event()
    adapter = OfflineAdapter(gate=gate)
    async with client_for(tmp_path, lambda: adapter) as client:
        response = await client.post("/api/jobs", json=REQUEST)
        assert response.status_code == 202
        initial = response.json()
        assert initial["state"] == "queued"
        assert initial["export_id"] is None
        job_id = initial["job_id"]
        await adapter.started.wait()
        running = (await client.get(f"/api/jobs/{job_id}")).json()
        assert running["state"] == "running"
        assert running["started_at"] is not None
        busy = await client.post("/api/jobs", json=REQUEST)
        assert busy.status_code == 429
        assert busy.json()["error"]["code"] == "job_capacity_reached"
        assert len(adapter.calls) == 1
        gate.set()
        result = await finished(client, job_id)
        assert result["state"] == "completed"
        assert result["error"] is None
        assert result["partial_export"] is False
        assert len(adapter.calls) == 2
        assert adapter.calls[1].occurrence == 1
        exported = await client.get(f"/api/exports/{result['export_id']}")
        assert exported.status_code == 200
        data = exported.json()["artifact"]
        assert data["manifest"]["experiment"]["task"] == orders.DEFAULT_TASK
        assert data["comparison"]["recovered"] is True
        assert (await client.get("/api/exports")).json()["exports"][0]["export_id"] == (
            result["export_id"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"scenario_id": "unknown"},
        {"scenario_id": "some.module:factory"},
        {"fault_type": "execute_code"},
        {"target_tool": "unknown_tool"},
        {"target_tool": "../../etc/passwd"},
        {"occurrence": 0},
        {"occurrence": 4},
        {"occurrence": True},
        {"occurrence": "1"},
        {"acknowledge_live_cost": False},
        {"acknowledge_live_cost": "true"},
        {"acknowledge_live_cost": 1},
        {"task": "arbitrary task"},
        {"adapter_factory": "some.module:factory"},
        {"artifact_root": "/private/secret"},
        {"parameters": {"payload": "client controlled"}},
    ],
)
async def test_invalid_configuration_never_executes(
    tmp_path: Path, change: dict
) -> None:
    def forbidden() -> FaultCapableAdapter:
        pytest.fail("invalid submission started execution")

    async with client_for(tmp_path, forbidden) as client:
        response = await client.post("/api/jobs", json=REQUEST | change)
        assert response.status_code == 422
        assert SECRET not in response.text
        assert "/private/secret" not in response.text
        missing = dict(REQUEST)
        del missing["acknowledge_live_cost"]
        assert (await client.post("/api/jobs", json=missing)).status_code == 422


@pytest.mark.asyncio
async def test_scenario_specific_fault_allowlist(tmp_path: Path) -> None:
    default = registered_scenarios()[0]
    scenario = Scenario(
        default.metadata.model_copy(update={"supported_fault_types": ("tool_error",)}),
        OfflineAdapter,
        default.evaluator,
    )
    app = create_app(tmp_path, scenarios=(scenario,))
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            response = await client.post(
                "/api/jobs", json=REQUEST | {"fault_type": "tool_timeout"}
            )
            assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_type", ["tool_timeout", "malformed_tool_output"])
async def test_supported_faults_use_server_defined_parameters(
    tmp_path: Path, fault_type: str
) -> None:
    adapter = OfflineAdapter()
    async with client_for(tmp_path, lambda: adapter) as client:
        response = await client.post(
            "/api/jobs", json=REQUEST | {"fault_type": fault_type}
        )
        result = await finished(client, response.json()["job_id"])
        assert result["state"] == "completed"
        fault = adapter.calls[1]
        assert fault.fault_type == fault_type
        assert fault.parameters == (
            {"timeout_ms": 10}
            if fault_type == "tool_timeout"
            else {"payload": '{"order_id":"123","status":'}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["baseline", "faulted"])
async def test_execution_failure_is_sanitized_and_partial_evidence_is_saved(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, failure: str
) -> None:
    async with client_for(tmp_path, lambda: OfflineAdapter(fail=failure)) as client:
        submitted = await client.post("/api/jobs", json=REQUEST)
        result = await finished(client, submitted.json()["job_id"])
        assert result["state"] == "failed"
        assert result["error"] == {
            "code": "execution_failed",
            "message": "Experiment execution failed.",
        }
        assert SECRET not in str(result) + caplog.text
        assert result["partial_export"] is (failure == "faulted")
        if failure == "faulted":
            artifact = (await client.get(f"/api/exports/{result['export_id']}")).json()[
                "artifact"
            ]
            assert artifact["faulted"]["status"] == "failed"
            assert artifact["comparison"]["recovered"] is None
        else:
            assert result["export_id"] is None


@pytest.mark.asyncio
async def test_timeout_preserves_cancelled_faulted_pair(tmp_path: Path) -> None:
    adapter = OfflineAdapter(gate=asyncio.Event(), fail="faulted_timeout")
    async with client_for(
        tmp_path, lambda: adapter, JobSettings(timeout_seconds=0.02)
    ) as client:
        submitted = await client.post("/api/jobs", json=REQUEST)
        result = await finished(client, submitted.json()["job_id"])
        assert result["state"] == "timed_out"
        assert result["error"]["code"] == "execution_timeout"
        assert result["cancellation_pending"] is False
        assert result["partial_export"] is True
        artifact = (await client.get(f"/api/exports/{result['export_id']}")).json()[
            "artifact"
        ]
        assert artifact["faulted"]["status"] == "cancelled"
        assert artifact["comparison"]["recovered"] is None


@pytest.mark.asyncio
async def test_timeout_does_not_release_slot_until_worker_stops(tmp_path: Path) -> None:
    gate = asyncio.Event()
    adapter = OfflineAdapter(gate=gate, resist_cancellation=True)
    async with client_for(
        tmp_path, lambda: adapter, JobSettings(timeout_seconds=0.02)
    ) as client:
        submitted = await client.post("/api/jobs", json=REQUEST)
        job_id = submitted.json()["job_id"]
        await asyncio.wait_for(adapter.cancelled.wait(), timeout=2)
        pending = (await client.get(f"/api/jobs/{job_id}")).json()
        assert pending["state"] == "timed_out"
        assert pending["cancellation_pending"] is True
        assert pending["finished_at"] is None
        assert (await client.post("/api/jobs", json=REQUEST)).status_code == 429
        gate.set()
        result = await finished(client, job_id)
        assert result["state"] == "timed_out"
        assert result["cancellation_pending"] is False
        assert result["export_id"] is not None  # observed late result, no promotion
        again = await client.post("/api/jobs", json=REQUEST)
        assert again.status_code == 202
        await finished(client, again.json()["job_id"])


@pytest.mark.asyncio
async def test_export_failure_has_no_exception_or_path_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise OSError(SECRET)

    monkeypatch.setattr(jobs, "export_experiment", broken)
    async with client_for(tmp_path) as client:
        submitted = await client.post("/api/jobs", json=REQUEST)
        result = await finished(client, submitted.json()["job_id"])
        assert result["state"] == "failed"
        assert result["error"]["code"] == "artifact_export_failed"
        assert result["export_id"] is None
        assert SECRET not in str(result) + caplog.text


@pytest.mark.asyncio
async def test_retention_unknown_jobs_and_browser_origin_controls(
    tmp_path: Path,
) -> None:
    async with client_for(tmp_path, settings=JobSettings(max_history=2)) as client:
        unknown = await client.get("/api/jobs/unknown")
        assert unknown.status_code == 404
        assert unknown.json()["error"]["code"] == "job_not_found"
        denied = await client.post(
            "/api/jobs", json=REQUEST, headers={"Origin": "https://untrusted.example"}
        )
        assert denied.status_code == 403
        assert "access-control-allow-origin" not in denied.headers
        rebound = await client.post(
            "/api/jobs",
            json=REQUEST,
            headers={"Host": "untrusted.example", "Origin": "http://untrusted.example"},
        )
        assert rebound.status_code == 400
        assert rebound.json()["error"]["code"] == "invalid_job_host"
        without_type = await client.post("/api/jobs", content=json.dumps(REQUEST))
        assert without_type.status_code == 415
        ids = []
        for _ in range(3):
            submitted = await client.post(
                "/api/jobs", json=REQUEST, headers={"Origin": "http://127.0.0.1"}
            )
            ids.append(submitted.json()["job_id"])
            await finished(client, ids[-1])
        assert (await client.get(f"/api/jobs/{ids[0]}")).status_code == 404
        assert (await client.get(f"/api/jobs/{ids[1]}")).status_code == 200
        assert len((await client.get("/api/exports")).json()["exports"]) == 3


@pytest.mark.asyncio
async def test_lifespan_shutdown_cancels_work_and_stops_submissions(
    tmp_path: Path,
) -> None:
    adapter = OfflineAdapter(gate=asyncio.Event())
    default = registered_scenarios()[0]
    app = create_app(
        tmp_path,
        scenarios=(Scenario(default.metadata, lambda: adapter, default.evaluator),),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as client:
        assert (await client.post("/api/jobs", json=REQUEST)).status_code == 503
        async with app.router.lifespan_context(app):
            submitted = await client.post("/api/jobs", json=REQUEST)
            await adapter.started.wait()
        result = (await client.get(f"/api/jobs/{submitted.json()['job_id']}")).json()
        assert result["state"] == "failed"
        assert result["error"]["code"] == "server_shutdown"
        assert result["finished_at"] is not None
        assert adapter.cancelled.is_set()
        assert (await client.post("/api/jobs", json=REQUEST)).status_code == 503


@pytest.mark.parametrize(
    "settings",
    [
        {"timeout_seconds": 0},
        {"timeout_seconds": float("nan")},
        {"timeout_seconds": 601},
        {"shutdown_grace_seconds": 31},
        {"max_history": 0},
        {"max_history": True},
    ],
)
def test_job_settings_are_bounded(settings: dict) -> None:
    with pytest.raises(ValueError):
        JobSettings(**settings)


@pytest.mark.asyncio
async def test_missing_credentials_fail_without_leaking_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app = create_app(tmp_path)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            malformed = await client.post(
                "/api/jobs",
                content="{invalid",
                headers={"Content-Type": "application/json"},
            )
            assert malformed.status_code == 422
            assert malformed.json()["error"]["code"] == "invalid_request"
            submitted = await client.post("/api/jobs", json=REQUEST)
            result = await finished(client, submitted.json()["job_id"])
            assert result["state"] == "failed"
            assert result["error"]["code"] == "execution_failed"
            assert "OPENAI_API_KEY" not in str(result) + caplog.text
            assert result["export_id"] is None


@pytest.mark.asyncio
async def test_queued_shutdown_never_constructs_adapter(tmp_path: Path) -> None:
    def forbidden() -> FaultCapableAdapter:
        pytest.fail("shutdown started a queued experiment")

    default = registered_scenarios()[0]
    manager = jobs.JobManager(
        tmp_path,
        (Scenario(default.metadata, forbidden, default.evaluator),),
        JobSettings(),
    )
    manager.start()
    submitted = manager.submit(jobs.JobRequest(**REQUEST))
    await manager.close()
    result = manager.get(submitted.job_id)
    assert result.state == "failed"
    assert result.error.code == "server_shutdown"
    assert result.started_at is None


@pytest.mark.asyncio
async def test_shutdown_grace_is_bounded_without_claiming_worker_stopped(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()
    adapter = OfflineAdapter(gate=gate, resist_cancellation=True)
    default = registered_scenarios()[0]
    manager = jobs.JobManager(
        tmp_path,
        (Scenario(default.metadata, lambda: adapter, default.evaluator),),
        JobSettings(shutdown_grace_seconds=0.005),
    )
    manager.start()
    submitted = manager.submit(jobs.JobRequest(**REQUEST))
    await adapter.started.wait()
    try:
        await asyncio.wait_for(manager.close(), timeout=1)
        pending = manager.get(submitted.job_id)
        assert pending.state == "failed"
        assert pending.cancellation_pending is True
        assert pending.finished_at is None
        with pytest.raises(RuntimeError, match="still stopping"):
            manager.start()
    finally:
        gate.set()
        await asyncio.wait_for(manager._active, timeout=2)
    assert manager.get(submitted.job_id).cancellation_pending is False


@pytest.mark.asyncio
async def test_artifact_write_is_off_loop_and_keeps_capacity_reserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    entered = threading.Event()
    release = threading.Event()
    original = jobs.export_experiment

    def blocked(*args, **kwargs):
        entered.set()
        if not release.wait(timeout=2):
            raise TimeoutError("test writer gate was not released")
        return original(*args, **kwargs)

    monkeypatch.setattr(jobs, "export_experiment", blocked)
    async with client_for(tmp_path) as client:
        submitted = await client.post("/api/jobs", json=REQUEST)
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            assert (await client.get("/api/health")).status_code == 200
            assert (await client.post("/api/jobs", json=REQUEST)).status_code == 429
            assert (await client.get("/api/exports")).json() == {"exports": []}
        finally:
            release.set()
        result = await finished(client, submitted.json()["job_id"])
        assert result["state"] == "completed"
