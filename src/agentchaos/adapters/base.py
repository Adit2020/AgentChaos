"""Minimal contract implemented by future framework adapters."""

from typing import Protocol

from agentchaos.models import AgentRun
from agentchaos.tracing import TraceRecorder


class AgentAdapter(Protocol):
    """Run one task, recording normalized events and returning its completed run.

    The caller supplies a fresh recorder for each run. Implementations own the
    recorder lifecycle: start it, record observations, then finish it.
    """

    async def run(self, task: str, *, recorder: TraceRecorder) -> AgentRun: ...
