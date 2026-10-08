"""Framework-neutral matching for the first deterministic tool fault."""

import pytest
from pydantic import ValidationError

from agentchaos.faults import FaultMatcher
from agentchaos.models import FaultSpec


def tool_fault(*, occurrence: int = 1) -> FaultSpec:
    return FaultSpec(
        fault_type="tool_error",
        target_tool="get_order_status",
        occurrence=occurrence,
        error_message="Temporary lookup failure. Please retry.",
    )


def test_target_and_first_occurrence_match_only_once() -> None:
    matcher = FaultMatcher(tool_fault())

    assert matcher.match_tool("another_tool") is None
    match = matcher.match_tool("get_order_status")
    assert match is not None
    assert match.target_tool == "get_order_status"
    assert match.occurrence == 1
    assert match.error_message == "Temporary lookup failure. Please retry."
    assert matcher.match_tool("get_order_status") is None


def test_later_occurrence_counts_only_target_invocations() -> None:
    matcher = FaultMatcher(tool_fault(occurrence=2))

    assert matcher.match_tool("get_order_status") is None
    assert matcher.match_tool("another_tool") is None
    assert matcher.match_tool("get_order_status") is not None
    assert matcher.match_tool("get_order_status") is None


def test_matchers_have_independent_state() -> None:
    spec = tool_fault()
    first = FaultMatcher(spec)
    second = FaultMatcher(spec)

    assert first.match_tool("get_order_status") is not None
    assert first.match_tool("get_order_status") is None
    assert second.match_tool("get_order_status") is not None


def test_tool_fault_requires_a_valid_target_occurrence_and_message() -> None:
    with pytest.raises(ValidationError):
        FaultSpec(fault_type="tool_error", target_tool="get_order_status")
    with pytest.raises(ValidationError):
        tool_fault(occurrence=0)
    with pytest.raises(ValueError, match="unsupported fault type"):
        FaultMatcher(FaultSpec(fault_type="future_fault", target_event_id="event-1"))
