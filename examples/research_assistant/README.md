# Research assistant example

This opt-in live example asks an OpenAI Agents SDK agent about fictional Project Atlas. It uses the existing `OpenAIAgentsAdapter` and `ExperimentRunner` to compare an ordinary run with one locally injected tool fault. No external search or database is used.

The local tools return deterministic JSON strings:

- `search_documents(query)` returns document IDs and short summaries for a named project or for documents matching all query terms.
- `get_document(document_id)` returns one full document or `found: false`.
- `get_project_details(project_name)` returns an independent structured project record or `found: false`.

The agent chooses which tools to call. The local data includes Atlas, Borealis, and Cedar. Atlas has two documents and a project record agreeing on launch date `2026-11-18`, owner `Maya Chen`, and status `approved`.

From the repository root, set `OPENAI_API_KEY` and run:

```sh
uv run python -m examples.research_assistant
```

Run one baseline/faulted pair with a first-occurrence fault on `get_document`:

```sh
AGENTCHAOS_COMPARE=1 AGENTCHAOS_FAULT=tool_error AGENTCHAOS_TARGET_TOOL=get_document uv run python -m examples.research_assistant
```

`AGENTCHAOS_FAULT` accepts `tool_error`, `tool_timeout`, or `malformed_tool_output`. `AGENTCHAOS_TARGET_TOOL` accepts any of the three tool names above. The matcher counts only calls to that tool. If the agent never calls it, `fault_triggered` is false. A timeout is a simulated tool-boundary delay; `AGENTCHAOS_TIMEOUT_MS` can set 0–250 ms. `AGENTCHAOS_MALFORMED_PAYLOAD` can override the default invalid JSON string.

Export a comparison to a new versioned directory under `artifacts/`:

```sh
AGENTCHAOS_COMPARE=1 AGENTCHAOS_EXPORT=1 AGENTCHAOS_FAULT=malformed_tool_output AGENTCHAOS_TARGET_TOOL=get_project_details uv run python -m examples.research_assistant
```

The SDK's Pydantic `ResearchAnswer` contract has three required string fields: `launch_date`, `owner`, and `status`. The status field's schema description asks for the source's lowercase wording; it stays a string rather than an enum so capitalization does not make a factual answer invalid before grading. The evaluator accepts `approved` and `Approved` as equivalent, but compares the full launch date and owner name exactly. Other status values, including `cancelled` and `not approved`, fail. A missing field, non-string value, or non-object final output is ungradable (`None`). Valid structure alone never establishes factual correctness.

The trace distinguishes fault delivery, model continuation, target-tool retries, and completed alternative-tool calls. Confirmed recovery requires a correct baseline and faulted answer, a delivered fault, continuation, and a successful correlated target-tool retry. A correct answer with no retry stays unknown, even if another tool returned after the fault: the normalized trace does not record which source facts caused the answer. Alternative-tool use is reported as evidence, not assumed to prove recovery.
