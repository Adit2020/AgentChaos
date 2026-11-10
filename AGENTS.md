# AgentChaos repository instructions

AgentChaos records normalized agent traces, reruns a task with one controlled local tool fault, and compares task success, recovery evidence, and execution cost. It also repeats baseline/faulted pairs sequentially for descriptive reliability statistics.

- Keep the models, tracing, faults, comparison, and orchestration under `src/agentchaos/` framework-neutral so other adapters can produce the same `AgentRun` events. SDK-specific imports and lifecycle handling belong in `src/agentchaos/adapters/openai_agents.py`.
- Keep fault state, recorders, and wrapped tools per run. The OpenAI adapter clones the agent and copies function tools for faulted runs; never mutate the caller's original agent or tools.
- Preserve call ID correlation, injected-fault metadata, and unknown outcomes when evidence is missing. A completed run or delivered malformed output alone does not prove recovery. See `.agents/skills/agentchaos-architecture/SKILL.md` before significant architecture changes.
- Add deterministic, offline tests under `tests/`. Use fake adapters or local SDK test doubles; never make a live model request in automated tests. `examples/order_support/` is the opt-in live example, so run it only with explicit authorization for paid API calls. Never commit or display API keys.

From the repository root, use Python 3.12+ and uv:

```sh
uv sync
uv run ruff format .
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

Use Serena to locate symbols and references before architectural edits; if indexing is unavailable, use its text search or `rg`. Use Context7 when current documentation for a non-OpenAI dependency is needed. Check current official OpenAI Developer Docs before relying on OpenAI Agents SDK behavior or changing its integration.

## Automatic commits and pushes

At the start of each new implementation task, check the current branch. If it is `main` or `master`, create an appropriately named feature branch before making any changes. Use a concise name with a task type prefix such as `feat/`, `fix/`, or `chore/`. If already on a feature branch, continue using it.

After each successfully completed implementation task:

1. Run all relevant available tests and validation checks.
2. Review `git status` and `git diff`.
3. Stage only files associated with the completed task.
4. Create a concise Conventional Commit message.
5. Push to the current feature branch on `origin`.
6. Report the commit hash, branch, tests, and push status.

Safety rules:

- Never push directly to `main` or `master`.
- Never force-push or discard unrelated work.
- Never commit secrets, credentials, or `.env` files.
- If tests fail or unexpected changes exist, stop and report.
- Do not automatically merge or deploy.
