"""Exercise the example's opt-in export without making a model request."""

from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from agentchaos.experiments import ExperimentOutcome
from agentchaos.multi_run import summarize_repetitions
from examples.order_support import __main__ as example
from tests.test_artifacts import load, sample_outcome


def configure_example(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> StringIO:
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=140)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "offline-placeholder")
    for name in (
        "AGENTCHAOS_COMPARE",
        "AGENTCHAOS_REPETITIONS",
        "AGENTCHAOS_MAX_REPETITIONS",
        "AGENTCHAOS_FAULT",
        "AGENTCHAOS_EXPORT",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(example, "Console", lambda: console)
    return output


@pytest.mark.asyncio
async def test_compare_mode_exports_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = configure_example(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTCHAOS_COMPARE", "1")
    monkeypatch.setenv("AGENTCHAOS_EXPORT", "1")

    async def fake_run(self: Any, **kwargs: Any) -> ExperimentOutcome:
        return sample_outcome()

    monkeypatch.setattr(example.ExperimentRunner, "run", fake_run)
    await example.main()

    exported = list((tmp_path / "artifacts").iterdir())
    assert len(exported) == 1
    assert load(exported[0] / "manifest.json")["artifact_type"] == "experiment"
    assert exported[0].name in output.getvalue().replace("\n", "")

    monkeypatch.delenv("AGENTCHAOS_EXPORT")
    monkeypatch.setattr(
        example,
        "export_experiment",
        lambda outcome: pytest.fail("export should be disabled"),
    )
    await example.main()
    assert len(list((tmp_path / "artifacts").iterdir())) == 1


@pytest.mark.asyncio
async def test_multi_run_mode_exports_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = configure_example(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTCHAOS_REPETITIONS", "3")
    monkeypatch.setenv("AGENTCHAOS_MAX_REPETITIONS", "3")
    monkeypatch.setenv("AGENTCHAOS_EXPORT", "1")

    async def fake_run(self: Any, **kwargs: Any):
        return summarize_repetitions(
            [sample_outcome()], requested_repetitions=3, stop_reason="requested"
        )

    monkeypatch.setattr(example.MultiRunRunner, "run", fake_run)
    await example.main()

    exported = next((tmp_path / "artifacts").iterdir())
    manifest = load(exported / "manifest.json")
    assert manifest["artifact_type"] == "multi_run"
    assert [item["state"] for item in manifest["repetitions"]] == [
        "recorded",
        "not_started",
        "not_started",
    ]
    assert "Artifacts exported to:" in output.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault_kind", [None, "tool_error"])
async def test_existing_direct_modes_do_not_export_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fault_kind: str | None,
) -> None:
    output = configure_example(monkeypatch, tmp_path)
    if fault_kind:
        monkeypatch.setenv("AGENTCHAOS_FAULT", fault_kind)
    calls: list[object] = []

    async def fake_run(
        self: Any, task: str, *, recorder: Any, fault: object = None
    ) -> Any:
        calls.append(fault)
        return sample_outcome().baseline

    monkeypatch.setattr(example.OpenAIAgentsAdapter, "run", fake_run)
    await example.main()

    assert len(calls) == 1
    assert (calls[0] is None) is (fault_kind is None)
    assert "Answer:" in output.getvalue()
    assert not (tmp_path / "artifacts").exists()


@pytest.mark.asyncio
async def test_export_requires_an_experiment_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure_example(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTCHAOS_EXPORT", "1")

    with pytest.raises(SystemExit, match="requires comparison or multi-run"):
        await example.main()

    assert not (tmp_path / "artifacts").exists()


@pytest.mark.asyncio
async def test_example_reports_export_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = configure_example(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTCHAOS_COMPARE", "1")
    monkeypatch.setenv("AGENTCHAOS_EXPORT", "1")

    async def fake_run(self: Any, **kwargs: Any) -> ExperimentOutcome:
        return sample_outcome()

    def failed_export(outcome: ExperimentOutcome) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(example.ExperimentRunner, "run", fake_run)
    monkeypatch.setattr(example, "export_experiment", failed_export)

    with pytest.raises(OSError, match="disk full"):
        await example.main()

    assert "Artifact export failed: disk full" in output.getvalue()


@pytest.mark.asyncio
async def test_interrupted_multi_run_exports_partial_report_and_preserves_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = configure_example(monkeypatch, tmp_path)
    monkeypatch.setenv("AGENTCHAOS_REPETITIONS", "3")
    monkeypatch.setenv("AGENTCHAOS_MAX_REPETITIONS", "3")
    monkeypatch.setenv("AGENTCHAOS_EXPORT", "1")
    original = RuntimeError("model unavailable")
    original.agentchaos_multi_run_report = summarize_repetitions(
        [sample_outcome()],
        requested_repetitions=3,
        incomplete_pairs=1,
        stop_reason="error",
    )

    async def fail_run(self: Any, **kwargs: Any) -> Any:
        raise original

    monkeypatch.setattr(example.MultiRunRunner, "run", fail_run)

    with pytest.raises(RuntimeError) as caught:
        await example.main()

    assert caught.value is original
    directory = next((tmp_path / "artifacts").iterdir())
    assert [
        item["state"] for item in load(directory / "manifest.json")["repetitions"]
    ] == [
        "recorded",
        "incomplete",
        "not_started",
    ]
    assert "Artifacts exported to:" in output.getvalue()
