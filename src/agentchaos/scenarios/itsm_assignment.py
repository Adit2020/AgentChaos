"""Server-owned ITSM assignment scenario configuration."""

import os

from agentchaos.adapters.base import FaultCapableAdapter


def create_adapter() -> FaultCapableAdapter:
    """Create a live-provider adapter after the existing job cost acknowledgment."""
    if not os.environ.get("OPENAI_API_KEY"):
        raise RuntimeError("OpenAI credentials are not configured")

    from agentchaos.benchmarks.itsm.agent import ITSMAssignmentAdapter

    model = os.environ.get("AGENTCHAOS_MODEL") or None
    return ITSMAssignmentAdapter(lambda: model)
