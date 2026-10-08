"""Keep the order-support JSON fault example's local contract explicit."""

import json

import pytest

from examples.order_support.__main__ import (
    OrderStatusRecord,
    get_order_status,
    get_order_status_json,
    selected_fault,
)


def test_json_tool_matches_documented_schema_and_malformed_default_does_not() -> None:
    record = OrderStatusRecord.model_validate_json(get_order_status_json("123"))
    fault = selected_fault("malformed_tool_output", "get_order_status_json")

    assert record.order_id == "123"
    assert record.status == "shipped"
    assert record.arrival_day == "Friday"
    schema = OrderStatusRecord.model_json_schema()
    assert set(schema["required"]) == {"order_id", "status", "arrival_day"}
    assert schema["additionalProperties"] is False
    with pytest.raises(json.JSONDecodeError):
        json.loads(fault.parameters["payload"])
    assert get_order_status("123") == "shipped, arriving Friday"
