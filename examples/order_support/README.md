# Order support example

Set `OPENAI_API_KEY`, then run `uv run python -m examples.order_support` from the project root. To inject one recoverable error into the first order lookup, run `AGENTCHAOS_FAULT=tool_error uv run python -m examples.order_support` instead. The local order dataset never calls an external order API.

Run `AGENTCHAOS_COMPARE=1 uv run python -m examples.order_support` to execute the same task twice and show a baseline versus faulted comparison. This mode makes two model-backed agent runs.

Set `AGENTCHAOS_FAULT` to `tool_error`, `tool_timeout`, or `malformed_tool_output` for a fault-only run, or alongside `AGENTCHAOS_COMPARE=1` to select the comparison fault. The default comparison fault is `tool_error`. The timeout is a simulated tool-boundary delay (`AGENTCHAOS_TIMEOUT_MS`, 0–250 ms); malformed mode injects a string instead of the JSON object normally returned by `get_order_status_json` (`AGENTCHAOS_MALFORMED_PAYLOAD`).
