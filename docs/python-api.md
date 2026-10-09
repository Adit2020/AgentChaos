# Python API

AgentChaos is an installable Python 3.12+ library. It is **not published to PyPI**. The package name remains `agentchaos`; install it from this checkout or a locally built wheel.

## Install

From the repository root, create an isolated environment and install the core package:

```sh
uv venv .venv
uv pip install --python .venv/bin/python .
```

Add the OpenAI Agents SDK integration when you need `OpenAIAgentsAdapter`:

```sh
uv pip install --python .venv/bin/python '.[openai]'
```

For a wheel, run `uv build --out-dir dist`, then install the generated wheel into another environment. For version 0.0.1:

```sh
uv venv /tmp/agentchaos-user
uv pip install --python /tmp/agentchaos-user/bin/python dist/agentchaos-0.0.1-py3-none-any.whl
uv pip install --python /tmp/agentchaos-user/bin/python 'dist/agentchaos-0.0.1-py3-none-any.whl[openai]'
```

Install the plain wheel for framework-neutral tracing and comparison. Install the `[openai]` extra to construct the optional adapter. The repository's `uv sync` installs development dependencies for its offline tests and bundled examples; those dependencies are not part of the core wheel.

## Imports and contracts

```python
from agentchaos import (
    AgentRun,
    ComparisonResult,
    EventKind,
    Experiment,
    ExperimentOutcome,
    ExperimentRunner,
    FaultSpec,
    MultiRunRunner,
    ReliabilityReport,
    RunEvent,
    RunStatus,
    TraceRecorder,
    compare_runs,
    required_facts,
)
from agentchaos.adapters import AgentAdapter, FaultCapableAdapter
from agentchaos.adapters.openai_agents import OpenAIAgentsAdapter  # [openai] extra
from agentchaos.artifacts import export_experiment, export_multi_run
```

The existing `agentchaos.models`, `agentchaos.experiments`, `agentchaos.multi_run`, `agentchaos.compare`, and `agentchaos.tracing` import paths remain available. `AgentAdapter` and `FaultCapableAdapter` are protocols for developers implementing an adapter. They emit framework-neutral `AgentRun` records through a fresh `TraceRecorder` per execution. Internal fault matching and SDK hooks are implementation details.

## One baseline/faulted experiment

The following example uses an existing SDK agent. This run makes live model requests; set `OPENAI_API_KEY` before executing it.

```python
import asyncio
from uuid import uuid4

from agents import Agent, function_tool
from pydantic import BaseModel

from agentchaos import Experiment, ExperimentRunner, FaultSpec, required_facts
from agentchaos.adapters.openai_agents import OpenAIAgentsAdapter
from agentchaos.artifacts import export_experiment


class LookupAnswer(BaseModel):
    status: str


@function_tool
def lookup(code: str) -> str:
    """Look up a local code."""
    return "ready" if code == "A1" else "unknown"


async def main() -> None:
    agent = Agent(
        name="Lookup agent",
        instructions="Use lookup to check A1, then answer with the returned status.",
        tools=[lookup],
        output_type=LookupAnswer,
    )
    experiment = Experiment(
        experiment_id=uuid4().hex,
        baseline_run_id=uuid4().hex,
        task="What is the status of A1?",
        fault=FaultSpec(
            fault_type="tool_error",
            target_tool="lookup",
            occurrence=1,
            error_message="Temporary lookup failure. Please retry.",
        ),
    )
    adapter = OpenAIAgentsAdapter(agent)
    outcome = await ExperimentRunner().run(
        adapter=adapter,
        experiment=experiment,
        evaluator=lambda run: required_facts(run, {"status": "ready"}),
    )
    print(outcome.comparison.fault_triggered)
    print(outcome.comparison.recovered)
    print(outcome.comparison.baseline_task_success)
    print(outcome.comparison.faulted_task_success)
    print(outcome.comparison.model_call_delta)
    print(outcome.comparison.tool_call_delta)
    print(export_experiment(outcome))


asyncio.run(main())
```

`FaultSpec` selects a named tool and occurrence (default 1). The supported fault types are `tool_error`, `tool_timeout` (optional `parameters={"timeout_ms": 10}`, range 0–250), and `malformed_tool_output` (required string `parameters={"payload": "..."}`). The chosen target must be a local SDK `FunctionTool`. The agent may choose different tools or none, in which case `fault_triggered` is false.

`ExperimentRunner.run` is async and accepts `adapter`, `experiment`, optional `task`, and either `expected_output` or a synchronous `evaluator`. An evaluator receives an `AgentRun` and returns `True`, `False`, or `None` for ungradable. The framework-neutral `required_facts(run, expected, case_insensitive_fields=())` grades complete string fields; case-insensitivity is opt-in per field. An `ExperimentOutcome` contains the prepared experiment, baseline and faulted `AgentRun`s, and `ComparisonResult`.

Inspect `AgentRun.events` for ordered `RunEvent`s. `EventKind` distinguishes run, model, and tool boundaries. `call_id` correlates each request/call with its response/result/error. `ComparisonResult` reports task success, fault delivery, continuation, retries, completed alternative-tool calls, recovery verdict, call deltas, and duration delta. `recovered=None` means the trace does not support a definite verdict; a correct final answer alone does not prove recovery.

## Repetitions and export

Inside `main` in the preceding example, reuse its adapter and experiment:

```python
from agentchaos import MultiRunRunner, required_facts
from agentchaos.artifacts import export_multi_run

report = await MultiRunRunner(max_repetitions=3).run(
    adapter=adapter,
    experiment=experiment,
    repetitions=3,
    evaluator=lambda run: required_facts(run, {"status": "ready"}),
)
print(report.confirmed_recovery_rate)
print(export_multi_run(report, experiment=experiment))
```

Repetitions run sequentially. `max_repetitions` is required and capped at 20; each repetition executes a baseline and a faulted run. The returned `ReliabilityReport` contains individual outcomes and aggregate rates with explicit denominators. Both export functions write versioned JSON, accept an optional `root` directory, and return the created path.

The complete [external integration example](../examples/external_integration/__main__.py) shows a structured answer, deterministic local tool, comparison inspection, and optional export.

Run it from the repository root after `uv sync` and setting `OPENAI_API_KEY` (this makes live model requests):

```sh
uv run python -m examples.external_integration
AGENTCHAOS_EXPORT=1 uv run python -m examples.external_integration
```
