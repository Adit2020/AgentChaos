# AgentChaos

AgentChaos is an early v0 Python framework for trace-driven reliability testing of AI agents.

The intended workflow is to run an agent normally and record a normalized **baseline** trace, rerun the same task with a controlled failure at a model or tool boundary, then **compare** the runs to see whether the agent recovered and what recovery cost it incurred.

This first version contains only framework-neutral domain models. It does not run agents, call model APIs, inject faults, or compare traces yet.

## Development

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required.

```sh
uv sync
uv run ruff check .
uv run pytest
```
