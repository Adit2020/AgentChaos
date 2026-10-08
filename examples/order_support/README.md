# Order support example

Set `OPENAI_API_KEY`, then run `uv run python -m examples.order_support` from the project root. To inject one recoverable error into the first order lookup, run `AGENTCHAOS_FAULT=tool_error uv run python -m examples.order_support` instead. The local order dataset never calls an external order API.

Run `AGENTCHAOS_COMPARE=1 uv run python -m examples.order_support` to execute the same task twice and show a baseline versus faulted comparison. This mode makes two model-backed agent runs.
