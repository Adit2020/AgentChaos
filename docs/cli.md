# Command-line experiments

The `agentchaos` command runs a developer-owned, local Python adapter through the same runners and artifact writer as the Python API. It does not publish results or implement reliability thresholds.

## Install and run

From this checkout, `uv sync` installs the CLI, development dependencies, and the local [external example project](../examples/external_integration/pyproject.toml). A developer installing only the built AgentChaos wheel can use `uv pip install --python /path/to/venv/bin/python dist/agentchaos-0.0.1-py3-none-any.whl`; install the example project separately in the same environment to run its sample config. Other developers install their own adapter packages instead. The CLI is installed as an executable in that environment's `bin` directory. AgentChaos is not yet published to PyPI.

For a clean wheel environment containing the bundled example, build both workspace packages and install both wheels:

```sh
uv build --all-packages --out-dir dist
uv pip install --python /path/to/venv/bin/python \
  dist/agentchaos-0.0.1-py3-none-any.whl \
  dist/agentchaos_example_external-0.0.1-py3-none-any.whl
```

```sh
agentchaos --help
agentchaos validate experiment.json
agentchaos run experiment.json
agentchaos run experiment.json --export
agentchaos run experiment.json --repetitions 3
```

Run the bundled developer example from the repository root after `uv sync`:

```sh
uv run agentchaos validate examples/external_integration/experiment.json
uv run agentchaos run examples/external_integration/experiment.json
uv run agentchaos run examples/external_integration/experiment.json --export
uv run agentchaos run examples/external_integration/experiment.json --repetitions 3
```

The `run` commands above make live model requests. Set `OPENAI_API_KEY` in the environment before running them; never put keys in JSON. `validate` reads only JSON and does not import the agent module, contact an API, or write artifacts. A valid config does not prove the adapter module is installed or the runtime credentials are available.

## Configuration version 1

The [sample config](../examples/external_integration/experiment.json) uses this shape:

```json
{
  "schema_version": 1,
  "task": "Is SKU-7 in stock?",
  "adapter_factory": "agentchaos_example_external:create_adapter",
  "fault": {
    "fault_type": "tool_error",
    "target_tool": "get_inventory",
    "occurrence": 1,
    "error_message": "Temporary inventory failure. Please retry."
  },
  "evaluation": {
    "type": "exact",
    "expected_output": {"product_id": "SKU-7", "in_stock": true}
  }
}
```

`schema_version` is required and independent of the JSON artifact schema version. The CLI accepts only `tool_error`, `tool_timeout`, and `malformed_tool_output`. `target_tool` is a local tool name; `occurrence` defaults to 1 and counts only calls to that tool. A tool error requires `error_message` and no parameters. A timeout accepts optional `error_message` and `parameters: {"timeout_ms": 10}` (integer 0–250). Malformed output requires `parameters: {"payload": "nonempty string"}` and does not use `error_message`. Unknown fields and unsupported parameter names are rejected.

`evaluation` is required. Use `{"type": "exact", "expected_output": <JSON value>}` to compare the complete final output. For structured string facts, use:

```json
{
  "type": "required_facts",
  "facts": {"launch_date": "2026-11-18", "status": "approved"},
  "case_insensitive_fields": ["status"]
}
```

The CLI passes this to the existing `required_facts` evaluator. Missing or non-string required fields are ungradable; wrong complete values fail. Case-insensitive comparison applies only to listed fields. A completed agent task failure is reported in the result and still exits successfully.

## Trusted adapter factory

`adapter_factory` is a `module.path:callable_name` reference. The module must be installed or otherwise importable in the Python environment that runs the CLI. The callable must be synchronous, take no arguments, and return an object with an async `run(task, *, recorder, fault)` method compatible with `FaultCapableAdapter`. The [external example's factory](../examples/external_integration/src/agentchaos_example_external/__init__.py) constructs an `OpenAIAgentsAdapter` from its own agent. Its separate project is installed by `uv sync` through the development dependency group; a clean wheel installation requires installing that project (or another developer adapter) explicitly. The existing standalone Python example uses the same factory.

A console-script executable does not rely on the source checkout being on Python's import path. Keeping the adapter in an installable developer package makes the same module reference work from the repository or another working directory, without changing the CLI's import rules.

**Only run configurations that reference trusted local Python modules.** Importing the module and calling its factory executes developer code. The JSON contains a module reference, never an expression or command. The CLI does not evaluate code strings, run configuration-provided shell commands, install dependencies, or fetch remote code. Keep credentials in the process environment or another mechanism owned by the adapter.

## Results, export, and failures

One `run` command executes a baseline/faulted pair and prints task success, fault delivery, recovery, retries, and cost deltas. `--repetitions N` runs N pairs sequentially, prints the planned `N` baseline plus `N` faulted executions before starting, then shows aggregate statistics and each recorded pair. N must be 1–20. `--export` writes versioned JSON to a new directory under `artifacts/` and prints its path; previous exports are never overwritten. The CLI reports partial results when the runner supplies them after a failure.

| Exit code | Meaning |
| --- | --- |
| 0 | Command completed, even if the evaluated agent answer was wrong or recovery was unknown |
| 2 | Invalid command input, missing file, malformed JSON, or invalid configuration |
| 3 | Adapter module/factory could not be imported, constructed, or checked |
| 4 | Experiment or export failed at runtime |

For v0, factories are synchronous, the CLI has no CI threshold gate, and the adapter module must already be importable locally. A tool fault may not fire if the agent never calls its target. Recovery stays unknown when correlated trace evidence is insufficient. The CLI adds no new fault type or agent framework.
