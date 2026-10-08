"""Deterministic asynchronous adapter used only by tests."""

import asyncio

from agentchaos import AgentRun, RunStatus
from agentchaos.tracing import TraceRecorder


class FakeAdapter:
    """Emit a model call and a tool call without external dependencies."""

    async def run(self, task: str, *, recorder: TraceRecorder) -> AgentRun:
        recorder.start(task=task, adapter="fake")
        recorder.record_model_request(call_id="model-1", name="fake-model")
        await asyncio.sleep(0)
        recorder.record_model_response(
            call_id="model-1", attributes={"text": "look up the order"}
        )
        recorder.record_tool_call(
            call_id="tool-1",
            name="lookup_order",
            attributes={"order_id": "123"},
        )
        await asyncio.sleep(0)
        recorder.record_tool_result(call_id="tool-1", attributes={"status": "shipped"})
        return recorder.finish(RunStatus.SUCCEEDED, output={"status": "shipped"})
