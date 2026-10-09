"""Portable, deterministic evaluators for structured final answers."""

from collections.abc import Collection, Mapping

from agentchaos.models import AgentRun


def required_facts(
    run: AgentRun,
    expected: Mapping[str, str],
    *,
    case_insensitive_fields: Collection[str] = (),
) -> bool | None:
    """Grade required string fields; return unknown for an ungradable shape.

    Callers define the required fields and canonical values. Missing fields,
    non-string values, and non-object output are ungradable; present but wrong
    strings fail. Only explicitly selected fields ignore case. Extra fields do
    not affect the required assertions.
    """

    if not expected:
        raise ValueError("at least one expected fact is required")
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in expected.items()
    ):
        raise TypeError("expected facts must map string fields to string values")
    unknown_fields = set(case_insensitive_fields) - expected.keys()
    if unknown_fields:
        raise ValueError("case-insensitive fields must be expected facts")
    if not isinstance(run.output, dict):
        return None
    if any(not isinstance(run.output.get(field), str) for field in expected):
        return None
    return all(
        run.output[field].casefold() == value.casefold()
        if field in case_insensitive_fields
        else run.output[field] == value
        for field, value in expected.items()
    )
