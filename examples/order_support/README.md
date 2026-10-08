# Order support example

Set `OPENAI_API_KEY`, then run `uv run python -m examples.order_support` from the project root. To inject one recoverable error into the first order lookup, run `AGENTCHAOS_FAULT=tool_error uv run python -m examples.order_support` instead. The local order dataset never calls an external order API.
