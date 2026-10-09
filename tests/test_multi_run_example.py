"""The live example's multi-run path is rendered without an API request."""

from io import StringIO
from typing import Any

import pytest
from rich.console import Console

from agentchaos.multi_run import summarize_repetitions
from examples.order_support import __main__ as example


@pytest.mark.asyncio
async def test_example_announces_agent_executions_before_starting(
    monkeypatch: Any,
) -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-placeholder")
    monkeypatch.setenv("AGENTCHAOS_REPETITIONS", "3")
    monkeypatch.setenv("AGENTCHAOS_MAX_REPETITIONS", "3")
    monkeypatch.delenv("AGENTCHAOS_COMPARE", raising=False)
    monkeypatch.delenv("AGENTCHAOS_FAULT", raising=False)
    monkeypatch.setattr(example, "Console", lambda: console)

    async def fake_run(self: Any, **kwargs: Any):
        assert "3 repetitions: 6 agent executions" in output.getvalue()
        assert kwargs["repetitions"] == 3
        return summarize_repetitions(
            [], requested_repetitions=3, stop_reason="requested"
        )

    monkeypatch.setattr(example.MultiRunRunner, "run", fake_run)
    await example.main()

    assert "AgentChaos reliability summary" in output.getvalue()
    assert "Repetition outcomes" in output.getvalue()
    assert "not started" in output.getvalue()
