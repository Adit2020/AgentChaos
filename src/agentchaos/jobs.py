"""Bounded, in-memory execution jobs for one local server event loop."""

import asyncio
import math
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from agentchaos.artifacts import export_experiment
from agentchaos.experiments import ExperimentOutcome, ExperimentRunner
from agentchaos.models import Experiment
from agentchaos.scenarios import Scenario, ToolFaultType, configured_fault


class JobRequest(BaseModel):
    """Clients select only registered options, never prompts or executable code."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    fault_type: ToolFaultType
    target_tool: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
    occurrence: int = Field(default=1, ge=1, le=10, strict=True)
    acknowledge_live_cost: bool = Field(strict=True)

    @field_validator("acknowledge_live_cost")
    @classmethod
    def require_acknowledgment(cls, value: bool) -> bool:
        if not value:
            raise ValueError("live execution cost acknowledgment is required")
        return value


class JobFailure(BaseModel):
    code: str
    message: str


class JobStatus(BaseModel):
    job_id: str
    scenario_id: str
    state: Literal["queued", "running", "completed", "failed", "timed_out"]
    submitted_at: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None
    export_id: str | None = None
    partial_export: bool = False
    cancellation_pending: bool = False
    error: JobFailure | None = None


@dataclass(frozen=True, slots=True)
class JobSettings:
    timeout_seconds: float = 120
    shutdown_grace_seconds: float = 5
    max_history: int = 100

    def __post_init__(self) -> None:
        for value, maximum in (
            (self.timeout_seconds, 600),
            (self.shutdown_grace_seconds, 30),
        ):
            if not math.isfinite(value) or not 0 < value <= maximum:
                raise ValueError("job time limits must be finite and bounded")
        if type(self.max_history) is not int or not 1 <= self.max_history <= 1000:
            raise ValueError("job history must contain between 1 and 1000 entries")


class JobServiceError(Exception):
    """An explicitly safe public error; never constructed from adapter errors."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class JobManager:
    """Reserve one slot atomically, supervise execution, and retain bounded status.

    All methods run on the application's event loop. Cancellation is cooperative:
    a timed-out worker continues occupying the slot until it actually settles.
    """

    def __init__(
        self, root: Path, scenarios: Sequence[Scenario], settings: JobSettings
    ) -> None:
        self.scenarios = {item.metadata.scenario_id: item for item in scenarios}
        if len(self.scenarios) != len(scenarios):
            raise ValueError("scenario IDs must be unique")
        self.root = root
        self.settings = settings
        self._jobs: OrderedDict[str, JobStatus] = OrderedDict()
        self._active: asyncio.Task[None] | None = None
        self._worker: asyncio.Task[ExperimentOutcome] | None = None
        self._current: JobStatus | None = None
        self._accepting = False

    def start(self) -> None:
        if self._worker is not None and not self._worker.done():
            raise RuntimeError("previous execution is still stopping")
        self._accepting = True

    async def close(self) -> None:
        self._accepting = False
        if self._active is None or self._active.done():
            return
        if self._current is not None and self._current.state in {"queued", "running"}:
            self._fail(self._current, "server_shutdown", "Server is stopping.")
        if self._worker is not None and not self._worker.done():
            self._current.cancellation_pending = True
            self._worker.cancel()
        # Do not wait indefinitely for a cancellation-resistant adapter. Keep
        # references and the slot reserved; process exit remains the hard stop.
        await asyncio.wait({self._active}, timeout=self.settings.shutdown_grace_seconds)

    def submit(self, request: JobRequest) -> JobStatus:
        scenario = self.scenarios.get(request.scenario_id)
        if scenario is None or (
            request.fault_type not in scenario.metadata.supported_fault_types
            or request.target_tool not in scenario.metadata.supported_target_tools
            or request.occurrence > scenario.metadata.max_occurrence
        ):
            raise JobServiceError(
                422,
                "invalid_scenario_configuration",
                "Scenario configuration is invalid.",
            )
        if not self._accepting:
            raise JobServiceError(
                503, "jobs_unavailable", "Job service is unavailable."
            )
        if self._active is not None and not self._active.done():
            raise JobServiceError(
                429,
                "job_capacity_reached",
                "An experiment is already active. Try later.",
            )
        while len(self._jobs) >= self.settings.max_history:
            self._jobs.popitem(last=False)
        job = JobStatus(
            job_id=uuid4().hex,
            scenario_id=request.scenario_id,
            state="queued",
            submitted_at=datetime.now(UTC),
        )
        self._jobs[job.job_id] = job
        self._current = job
        self._active = asyncio.create_task(self._run(job, scenario, request))
        return job.model_copy(deep=True)

    def get(self, job_id: str) -> JobStatus:
        job = self._jobs.get(job_id)
        if job is None:
            raise JobServiceError(404, "job_not_found", "Job not found or expired.")
        return job.model_copy(deep=True)

    @staticmethod
    def _fail(job: JobStatus, code: str, message: str) -> None:
        job.state = "failed"
        job.error = JobFailure(code=code, message=message)

    async def _execute(
        self, scenario: Scenario, request: JobRequest
    ) -> ExperimentOutcome:
        experiment = Experiment(
            experiment_id=uuid4().hex,
            baseline_run_id=uuid4().hex,
            task=scenario.metadata.default_task,
            fault=configured_fault(
                request.fault_type, request.target_tool, request.occurrence
            ),
        )
        return await ExperimentRunner().run(
            adapter=scenario.adapter_factory(),
            experiment=experiment,
            evaluator=scenario.evaluator,
        )

    async def _save(self, job: JobStatus, outcome: ExperimentOutcome) -> None:
        # Filesystem I/O must not block the API's event loop. Shield the write
        # so shutdown cancellation cannot release the slot while it still runs.
        saving = asyncio.create_task(
            asyncio.to_thread(export_experiment, outcome, root=self.root)
        )
        try:
            try:
                directory = await asyncio.shield(saving)
            except asyncio.CancelledError:
                directory = await saving
            job.export_id = directory.name
        except Exception:
            if job.error is None:
                self._fail(
                    job, "artifact_export_failed", "Artifacts could not be saved."
                )

    async def _run(
        self, job: JobStatus, scenario: Scenario, request: JobRequest
    ) -> None:
        try:
            if not self._accepting:
                return
            job.state = "running"
            job.started_at = datetime.now(UTC)
            worker = asyncio.create_task(self._execute(scenario, request))
            self._worker = worker
            try:
                done, _ = await asyncio.wait(
                    {worker}, timeout=self.settings.timeout_seconds
                )
            except asyncio.CancelledError:
                self._fail(job, "server_shutdown", "Server is stopping.")
                done = set()
            if not done:
                if job.state == "running":
                    job.state = "timed_out"
                    job.error = JobFailure(
                        code="execution_timeout",
                        message="Execution deadline exceeded; cancellation requested.",
                    )
                job.cancellation_pending = True
                worker.cancel()

            outcome = None
            try:
                outcome = await worker
            except (Exception, asyncio.CancelledError) as exc:
                if job.state == "running":
                    self._fail(job, "execution_failed", "Experiment execution failed.")
                partial = getattr(exc, "agentchaos_experiment", None)
                if isinstance(partial, ExperimentOutcome):
                    outcome = partial
                    job.partial_export = True
            job.cancellation_pending = False
            if outcome is not None:
                await self._save(job, outcome)
            if job.state == "running":
                job.state = "completed"
        finally:
            job.finished_at = datetime.now(UTC)
            self._current = None
            # Preserve the worker reference if forced event-loop shutdown
            # interrupts supervision before cooperative cancellation settles.
            if self._worker is not None and self._worker.done():
                self._worker = None
