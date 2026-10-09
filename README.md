# AgentChaos

AgentChaos is an early v0 Python framework for trace-driven reliability testing of AI agents.

The intended workflow is to run an agent normally and record a normalized **baseline** trace, rerun the same task with a controlled failure at a model or tool boundary, then **compare** the runs to see whether the agent recovered and what recovery cost it incurred.

This early version contains framework-neutral domain models, an async adapter contract, and a single-run trace recorder. The first real adapter observes OpenAI Agents SDK runs through per-run hooks and produces normalized `AgentRun` objects.

AgentChaos supports one deterministic local tool fault per run: `tool_error`, `tool_timeout`, or `malformed_tool_output`. It can compare one baseline and one faulted run, or repeat that pair sequentially and summarize the observed outcomes.

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

## Sequential repetitions

Set both `AGENTCHAOS_REPETITIONS` and `AGENTCHAOS_MAX_REPETITIONS` to run a bounded batch. The API defaults to three repetitions; each repetition runs one baseline and one faulted agent execution. Ten repetitions can therefore start 20 agent executions, each of which may make multiple model requests. The maximum must be explicit and cannot exceed 20.

```sh
AGENTCHAOS_REPETITIONS=3 AGENTCHAOS_MAX_REPETITIONS=3 AGENTCHAOS_FAULT=tool_error uv run python -m examples.order_support
```

`MultiRunRunner(max_repetitions=...).run(..., repetitions=3)` reuses `ExperimentRunner` for each pair. It runs sequentially and checks an optional `asyncio.Event` between pairs for a clean stop. Unexpected errors stop the batch and re-raise the original exception with the partial report attached as `agentchaos_multi_run_report`; missing runs are counted as incomplete rather than invented.

The fault trigger rate uses completed pairs as its denominator. The confirmed recovery rate uses only triggered pairs with two successful run statuses and a known recovery verdict; a separate requested-repetition rate uses all requested pairs. Unknown recovery counts cover completed pairs without a confirmed verdict; incomplete and unstarted pairs are reported separately. Task success rates use only pairs with known evaluations. Call deltas use completed pairs, while latency statistics exclude missing durations and retain negative values. A negative latency delta is an observation, not proof that a fault improved performance. The report gives observation counts for each statistic and does not estimate API cost.

## JSON artifacts

Add `AGENTCHAOS_EXPORT=1` to a comparison or multi-run example command to save its normalized results under a new, UTC-stamped directory in `artifacts/` (gitignored):

```sh
AGENTCHAOS_EXPORT=1 AGENTCHAOS_COMPARE=1 uv run python -m examples.order_support
AGENTCHAOS_EXPORT=1 AGENTCHAOS_REPETITIONS=3 AGENTCHAOS_MAX_REPETITIONS=3 uv run python -m examples.order_support
```

A single experiment exports `manifest.json`, `baseline.json`, `faulted.json`, and `comparison.json`. A multi-run export adds `summary.json` and one `repetitions/NNN/` directory per recorded pair, containing its experiment configuration, two run traces, and comparison. The manifest names incomplete and unstarted repetitions without inventing their missing traces. It is written last and marks an export as complete; a failed write raises an error and removes its unfinished directory.

Every JSON file includes `schema_version: 1`; readers should check it, and incompatible format changes will increment it. Run, comparison, and summary files wrap normalized data in `data`. Programmatic callers can use `agentchaos.artifacts.export_experiment(outcome)` or `export_multi_run(report, experiment=experiment)`, with an optional `root` path. Open a file with any JSON viewer, or run `python -m json.tool artifacts/YOUR_EXPORT_DIRECTORY/manifest.json` after replacing the directory name. Version 1 stores the existing normalized fields only; it does not capture raw SDK objects, full model traffic, or credentials from the environment. Obvious credential-named fields are redacted in exported JSON. Application-specific tasks, outputs, fault payloads, and trace attributes can still be sensitive. Review them before sharing artifacts.
