# AgentChaos

AgentChaos is an early v0 Python framework for trace-driven reliability testing of AI agents.

The intended workflow is to run an agent normally and record a normalized **baseline** trace, rerun the same task with a controlled failure at a model or tool boundary, then **compare** the runs to see whether the agent recovered and what recovery cost it incurred.

This early version contains framework-neutral domain models, an async adapter contract, and a single-run trace recorder. The first real adapter observes OpenAI Agents SDK runs through per-run hooks and produces normalized `AgentRun` objects.

AgentChaos supports one deterministic local tool fault per run: `tool_error`, `tool_timeout`, or `malformed_tool_output`. It can compare one baseline and one faulted run; campaigns and aggregate statistics are not part of this early version.

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

Select a fault-only run with `AGENTCHAOS_FAULT=tool_error`, `AGENTCHAOS_FAULT=tool_timeout`, or `AGENTCHAOS_FAULT=malformed_tool_output` before `uv run python -m examples.order_support`. AgentChaos wraps a per-run copy of the function tool, so the application tool code and original agent remain unchanged.

To run an automatic baseline and faulted comparison, use `AGENTCHAOS_COMPARE=1 uv run python -m examples.order_support`. Add `AGENTCHAOS_FAULT=tool_timeout` or `AGENTCHAOS_FAULT=malformed_tool_output` to select either new fault; the comparison default remains `tool_error`. The example evaluates both final answers with a deterministic status check, then reports whether the fault fired, whether the task passed, and the extra calls and time.

`tool_timeout` is a bounded, simulated tool-boundary timeout rather than a real network or socket timeout. Set `AGENTCHAOS_TIMEOUT_MS` to an integer from 0 to 250 (default 10). The malformed mode uses a JSON-returning local tool whose expected output is an object with string `order_id` and `status`, and string or null `arrival_day`; set `AGENTCHAOS_MALFORMED_PAYLOAD` to choose the injected string. A delivered malformed result proves injection, not that the agent recognized it. Recovery remains unknown without task success and a post-fault retry.

Programmatic comparisons accept either an exact `expected_output` or a synchronous `evaluator` callback on each `AgentRun`. Call counts represent attempted model and tool calls, including calls whose terminal event is missing.
