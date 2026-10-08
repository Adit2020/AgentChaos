"""Match a configured fault against observed tool invocations."""

from dataclasses import dataclass

from agentchaos.models import FaultSpec


@dataclass(frozen=True)
class FaultMatch:
    """The deterministic tool occurrence selected for a fault."""

    fault_type: str
    target_tool: str
    occurrence: int
    error_message: str | None
    timeout_ms: int = 0
    payload: str | None = None


class FaultMatcher:
    """Count matching tool invocations within one run only."""

    def __init__(self, spec: FaultSpec) -> None:
        if spec.fault_type not in {
            "tool_error",
            "tool_timeout",
            "malformed_tool_output",
        }:
            raise ValueError(f"unsupported fault type: {spec.fault_type}")
        if spec.target_tool is None:
            raise ValueError("runtime tool faults require target_tool")
        self._spec = spec.model_copy(deep=True)
        self._seen = 0

    def match_tool(self, tool_name: str) -> FaultMatch | None:
        """Return the selected occurrence once, without counting other tools."""

        if tool_name != self._spec.target_tool:
            return None
        self._seen += 1
        if self._seen != self._spec.occurrence:
            return None
        return FaultMatch(
            fault_type=self._spec.fault_type,
            target_tool=tool_name,
            occurrence=self._seen,
            error_message=(
                self._spec.error_message
                if self._spec.error_message is not None
                else (
                    "Tool timed out."
                    if self._spec.fault_type == "tool_timeout"
                    else None
                )
            ),
            timeout_ms=(
                self._spec.parameters.get("timeout_ms", 10)
                if self._spec.fault_type == "tool_timeout"
                else 0
            ),
            payload=(
                self._spec.parameters.get("payload")
                if self._spec.fault_type == "malformed_tool_output"
                else None
            ),
        )
