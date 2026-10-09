---
name: agentchaos-architecture
description: Use when planning or implementing significant AgentChaos architecture changes; preserve its framework-neutral trace contracts, per-run fault isolation, and evidence-based comparison.
---

# AgentChaos architecture

Inspect the current code and tests before changing boundaries. This map describes the implementation, not a mandate to add components.

## Data flow and ownership

1. `src/agentchaos/models.py` defines portable Pydantic `RunEvent`, `AgentRun`, `FaultSpec`, `Experiment`, and `ComparisonResult` records. Events have ordered sequences, timezone-aware timestamps, correlated `call_id`s, and JSON-compatible attributes. Core records must not contain SDK objects.
2. `src/agentchaos/adapters/base.py` defines async `AgentAdapter` and `FaultCapableAdapter` protocols. A caller supplies a fresh `TraceRecorder`; the adapter starts, records, and finishes its run. `src/agentchaos/tracing/recorder.py` enforces this lifecycle and returns detached event snapshots and `AgentRun`s.
3. `src/agentchaos/adapters/openai_agents.py` is the SDK boundary. Per-run `RunHooks` translate model and tool callbacks into normalized events. For a faulted execution it creates a fresh `FaultMatcher`, copies local `FunctionTool`s, wraps the selected invocation, and clones the agent. The original agent and tools remain unchanged. A failure or cancellation re-raises the original exception with a finished trace attached as `agentchaos_run` when available.
4. `src/agentchaos/faults/matching.py` selects a named tool occurrence within one run. The supported faults are `tool_error`, `tool_timeout`, and `malformed_tool_output`. The timeout is a bounded, cancellable simulated tool-boundary delay; a malformed payload is delivered as tool output. Neither invokes the original tool when selected. The adapter emits fault kind, target, occurrence, and injection/delivery metadata on the correlated terminal tool event.
5. `src/agentchaos/experiments.py` runs the same task first without a fault, then with a deep-copied fault and a separate recorder. It calls `src/agentchaos/compare/` on the resulting normalized traces, returning `ExperimentOutcome`. It preserves original exceptions and attaches a partial pair as `agentchaos_experiment` when both run traces exist. `src/agentchaos/multi_run.py` reuses `ExperimentRunner` sequentially with fresh IDs and a deep-copied experiment for each pair; its explicit maximum is capped at 20, and an optional stop event is checked between pairs. Errors stop the batch and attach `agentchaos_multi_run_report` where possible.
6. `src/agentchaos/compare/analyzer.py` counts model requests and tool calls once each, recognizes a configured fault only from coherent injected tool-call evidence, and evaluates final output with an exact expectation or synchronous callback. Confirmed recovery requires a triggered fault, a successful evaluated baseline and faulted task, and post-fault trace evidence; malformed output additionally requires a target-tool retry. Absent evaluation or ambiguous fault evidence leaves recovery unknown. `src/agentchaos/multi_run.py` aggregates known verdicts with explicit denominators, excluding failed or cancelled pairs from its confirmed recovery rate and keeping incomplete and unstarted pairs distinct.

## Extension rules

- Keep the models, tracing, fault matching, orchestration, and comparison under `src/agentchaos/` independent of the OpenAI SDK. New adapters should implement the existing protocol and emit the same event vocabulary; translate provider objects and IDs at the adapter boundary.
- Keep mutable matchers, hook state, wrapped tools, and recorders scoped to one execution. Do not modify shared SDK agent/tool instances or let a selected fault reach later runs.
- When changing trace or fault semantics, update the adapter emission and analyzer interpretation together. Preserve the distinction between fault delivery, agent detection, task success, and confirmed recovery. Natural tool errors must not count as injected faults.
- Reuse `ExperimentRunner` for repeated pairs instead of duplicating baseline/faulted logic. Preserve exceptions, cancellation, and partial evidence; do not invent missing runs or silently retry paid calls.
- Verify with deterministic offline tests in `tests/` for recorder isolation, fault targeting, SDK translation, comparison evidence, and multi-run denominators. `examples/order_support/` is a live opt-in demonstration, not an automated test target.
