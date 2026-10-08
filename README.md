# AgentChaos

AgentChaos is an early v0 Python framework for trace-driven reliability testing of AI agents.

The intended workflow is to run an agent normally and record a normalized **baseline** trace, rerun the same task with a controlled failure at a model or tool boundary, then **compare** the runs to see whether the agent recovered and what recovery cost it incurred.

This early version contains framework-neutral domain models, an async adapter contract, and a single-run trace recorder. The first real adapter observes OpenAI Agents SDK runs through per-run hooks and produces normalized `AgentRun` objects.

AgentChaos can now inject one deterministic local function-tool error per run. It does not yet include other faults or run comparison logic.

## Development

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required.

```sh
uv sync
uv run ruff check .
uv run pytest
```

## First example

The order-support example uses a local, hard-coded order lookup tool and makes a live model call.

```sh
uv sync
export OPENAI_API_KEY="your-api-key"
# Optional: export AGENTCHAOS_MODEL="a-model-you-can-access"
uv run python -m examples.order_support
```

For the first tool-error experiment, run `AGENTCHAOS_FAULT=tool_error uv run python -m examples.order_support` after setting `OPENAI_API_KEY`. AgentChaos wraps a per-run copy of the function tool, so the application tool code and original agent remain unchanged. The injected error is returned to the model as a tool result, allowing it to decide whether to retry.

To run an automatic baseline and faulted comparison, use `AGENTCHAOS_COMPARE=1 uv run python -m examples.order_support`. The example evaluates both final answers with a deterministic status check, then reports whether the fault fired, whether the task passed, and the extra calls and time. Recovery remains unknown when the answer or trace does not provide enough evidence.

Programmatic comparisons accept either an exact `expected_output` or a synchronous `evaluator` callback on each `AgentRun`. Call counts represent attempted model and tool calls, including calls whose terminal event is missing.
