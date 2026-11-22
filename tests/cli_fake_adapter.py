"""Developer-owned fake adapters used by offline CLI tests."""

from agentchaos import AgentRun, FaultSpec, RunStatus, TraceRecorder


class LocalAdapter:
    async def run(
        self, task: str, *, recorder: TraceRecorder, fault: FaultSpec | None = None
    ) -> AgentRun:
        recorder.start(task=task, adapter="cli-fake")
        recorder.record_model_request(call_id="model-1", name="fake")
        recorder.record_model_response(call_id="model-1")
        recorder.record_tool_call(call_id="tool-1", name="lookup")
        if fault is None:
            recorder.record_tool_result(call_id="tool-1")
        else:
            recorder.record_tool_result(
                call_id="tool-1",
                attributes={
                    "injected": True,
                    "success": False,
                    "fault_kind": fault.fault_type,
                    "target_tool": "lookup",
                    "occurrence": fault.occurrence,
                },
            )
            recorder.record_model_request(call_id="model-2", name="fake")
            recorder.record_model_response(call_id="model-2")
            recorder.record_tool_call(call_id="tool-2", name="lookup")
            recorder.record_tool_result(call_id="tool-2")
            recorder.record_model_request(call_id="model-3", name="fake")
            recorder.record_model_response(call_id="model-3")
        return recorder.finish(RunStatus.SUCCEEDED, output={"status": "ready"})


class IncorrectAdapter(LocalAdapter):
    async def run(
        self, task: str, *, recorder: TraceRecorder, fault: FaultSpec | None = None
    ) -> AgentRun:
        run = await super().run(task, recorder=recorder, fault=fault)
        return run.model_copy(update={"output": {"status": "wrong"}})


class FailingAdapter:
    async def run(
        self, task: str, *, recorder: TraceRecorder, fault: FaultSpec | None = None
    ) -> AgentRun:
        raise RuntimeError("local adapter stopped")


def create_adapter() -> LocalAdapter:
    return LocalAdapter()


def incorrect_adapter() -> IncorrectAdapter:
    return IncorrectAdapter()


def failing_adapter() -> FailingAdapter:
    return FailingAdapter()


def incompatible_adapter() -> object:
    return object()


async def async_factory() -> LocalAdapter:
    return LocalAdapter()


def raising_factory() -> LocalAdapter:
    raise RuntimeError("factory initialization failed")
