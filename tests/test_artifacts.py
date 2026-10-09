"""Offline tests for versioned, non-overwriting JSON result exports."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentchaos import (
    AgentRun,
    ComparisonResult,
    EventKind,
    Experiment,
    FaultSpec,
    RunEvent,
    RunStatus,
)
from agentchaos.artifacts import (
    SCHEMA_VERSION,
    export_experiment,
    export_multi_run,
    writer,
)
from agentchaos.experiments import ExperimentOutcome
from agentchaos.multi_run import summarize_repetitions

START = datetime(2026, 1, 1, tzinfo=UTC)
ATTRIBUTES = {
    "unicode": "café ☕",
    "nested": {"items": [None, True, 1.25, {"quote": 'a "value"'}]},
}


def sample_experiment(index: int = 1) -> Experiment:
    return Experiment(
        experiment_id=f"experiment-{index}",
        baseline_run_id=f"baseline-{index}",
        faulted_run_id=f"faulted-{index}",
        task="Where is order 123?",
        fault=FaultSpec(
            fault_type="tool_error",
            target_tool="get_order_status",
            error_message="Temporary failure",
        ),
    )


def sample_run(run_id: str, *, with_events: bool = True) -> AgentRun:
    events = (
        [
            RunEvent(
                event_id=f"{run_id}:0",
                sequence=0,
                occurred_at=START,
                kind=EventKind.RUN_STARTED,
            ),
            RunEvent(
                event_id=f"{run_id}:1",
                sequence=1,
                occurred_at=START + timedelta(milliseconds=10),
                kind=EventKind.TOOL_CALL,
                call_id="tool-1",
                name="get_order_status",
            ),
            RunEvent(
                event_id=f"{run_id}:2",
                sequence=2,
                occurred_at=START + timedelta(milliseconds=20),
                kind=EventKind.TOOL_RESULT,
                call_id="tool-1",
                attributes=ATTRIBUTES,
            ),
            RunEvent(
                event_id=f"{run_id}:3",
                sequence=3,
                occurred_at=START + timedelta(seconds=1),
                kind=EventKind.RUN_COMPLETED,
            ),
        ]
        if with_events
        else []
    )
    return AgentRun(
        run_id=run_id,
        task="Where is order 123?",
        adapter="fake",
        status=RunStatus.SUCCEEDED,
        started_at=START,
        ended_at=START + timedelta(seconds=1),
        events=events,
        output={"answer": "shipped"},
    )


def sample_outcome(
    index: int = 1, *, recovered: bool | None = None
) -> ExperimentOutcome:
    experiment = sample_experiment(index)
    return ExperimentOutcome(
        experiment=experiment,
        baseline=sample_run(experiment.baseline_run_id),
        faulted=sample_run(experiment.faulted_run_id),
        comparison=ComparisonResult(
            experiment_id=experiment.experiment_id,
            baseline_run_id=experiment.baseline_run_id,
            faulted_run_id=experiment.faulted_run_id,
            recovered=recovered,
            fault_triggered=True,
            baseline_model_calls=2,
            faulted_model_calls=1,
            model_call_delta=-1,
            baseline_tool_calls=1,
            faulted_tool_calls=2,
            tool_call_delta=1,
            duration_delta_ms=-120,
            baseline_task_success=True,
            faulted_task_success=None,
        ),
    )


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_single_export_is_versioned_readable_and_does_not_mutate(
    tmp_path: Path,
) -> None:
    outcome = sample_outcome()
    before = (
        outcome.experiment.model_dump(mode="json"),
        outcome.baseline.model_dump(mode="json"),
        outcome.faulted.model_dump(mode="json"),
        outcome.comparison.model_dump(mode="json"),
    )

    directory = export_experiment(outcome, root=tmp_path)

    assert directory.parent == tmp_path
    assert set(path.name for path in directory.iterdir()) == {
        "manifest.json",
        "baseline.json",
        "faulted.json",
        "comparison.json",
    }
    manifest = load(directory / "manifest.json")
    assert manifest["schema_version"] == SCHEMA_VERSION == 1
    assert manifest["artifact_type"] == "experiment"
    assert manifest["experiment"]["fault"]["target_tool"] == "get_order_status"
    assert manifest["baseline_status"] == "succeeded"
    assert manifest["recovered"] is None
    assert manifest["exported_at"].endswith("+00:00")
    for name, model in (
        ("baseline.json", AgentRun),
        ("faulted.json", AgentRun),
        ("comparison.json", ComparisonResult),
    ):
        document = load(directory / name)
        assert document["schema_version"] == SCHEMA_VERSION
        assert model.model_validate(document["data"])
    run_data = load(directory / "faulted.json")["data"]
    assert run_data["events"][2]["attributes"] == ATTRIBUTES
    assert run_data["events"][1]["call_id"] == "tool-1"
    comparison = load(directory / "comparison.json")["data"]
    assert comparison["duration_delta_ms"] == -120
    assert comparison["recovered"] is None
    assert before == (
        outcome.experiment.model_dump(mode="json"),
        outcome.baseline.model_dump(mode="json"),
        outcome.faulted.model_dump(mode="json"),
        outcome.comparison.model_dump(mode="json"),
    )


def test_multi_run_export_records_only_available_pairs(tmp_path: Path) -> None:
    outcomes = (sample_outcome(1, recovered=True), sample_outcome(2))
    report = summarize_repetitions(
        outcomes, requested_repetitions=4, incomplete_pairs=1, stop_reason="error"
    )

    directory = export_multi_run(report, experiment=sample_experiment(), root=tmp_path)

    manifest = load(directory / "manifest.json")
    summary = load(directory / "summary.json")
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["artifact_type"] == "multi_run"
    assert manifest["report_state"] == "error"
    assert [item["state"] for item in manifest["repetitions"]] == [
        "recorded",
        "recorded",
        "incomplete",
        "not_started",
    ]
    assert summary["record_type"] == "reliability_summary"
    assert summary["data"]["completed_repetitions"] == 2
    assert summary["data"]["incomplete_pairs"] == 1
    assert summary["data"]["average_latency_delta_ms"] == -120
    assert summary["data"]["unknown_recoveries"] == 1
    for index, outcome in enumerate(outcomes, start=1):
        repetition = directory / "repetitions" / f"{index:03d}"
        assert {path.name for path in repetition.iterdir()} == {
            "experiment.json",
            "baseline.json",
            "faulted.json",
            "comparison.json",
        }
        assert load(repetition / "experiment.json")["data"]["experiment_id"] == (
            outcome.experiment.experiment_id
        )
        assert load(repetition / "baseline.json")["data"]["run_id"] == (
            outcome.baseline.run_id
        )
    assert not (directory / "repetitions" / "003").exists()
    assert not (directory / "repetitions" / "004").exists()


def test_empty_report_and_incomplete_run_remain_explicit(tmp_path: Path) -> None:
    report = summarize_repetitions([], requested_repetitions=3, stop_reason="requested")
    directory = export_multi_run(report, experiment=sample_experiment(), root=tmp_path)
    assert not (directory / "repetitions").exists()
    assert [
        entry["state"] for entry in load(directory / "manifest.json")["repetitions"]
    ] == [
        "not_started",
        "not_started",
        "not_started",
    ]

    outcome = sample_outcome()
    running = AgentRun(
        run_id=outcome.faulted.run_id,
        task=outcome.faulted.task,
        adapter="fake",
        status=RunStatus.RUNNING,
        started_at=START,
        events=[],
    )
    incomplete = ExperimentOutcome(
        outcome.experiment, outcome.baseline, running, outcome.comparison
    )
    single = export_experiment(incomplete, root=tmp_path)
    faulted = load(single / "faulted.json")["data"]
    assert faulted["events"] == []
    assert faulted["ended_at"] is None
    assert load(single / "manifest.json")["faulted_status"] == "running"


def test_exports_get_unique_directories_and_preserve_prior_files(
    tmp_path: Path,
) -> None:
    outcome = sample_outcome()
    first = export_experiment(outcome, root=tmp_path)
    original = (first / "manifest.json").read_bytes()
    second = export_experiment(outcome, root=tmp_path)

    assert first != second
    assert (first / "manifest.json").read_bytes() == original
    assert len(list(tmp_path.iterdir())) == 2


def test_export_redacts_credential_fields_and_never_reads_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = sample_outcome()
    outcome.faulted.events[2].attributes["headers"] = {
        "Authorization": "Bearer trace-secret",
        "X-API-Key": "trace-api-secret",
        "content_type": "application/json",
    }
    monkeypatch.setenv("OPENAI_API_KEY", "environment-secret")

    directory = export_experiment(outcome, root=tmp_path)

    exported = load(directory / "faulted.json")["data"]
    headers = exported["events"][2]["attributes"]["headers"]
    assert headers == {
        "Authorization": "[REDACTED]",
        "X-API-Key": "[REDACTED]",
        "content_type": "application/json",
    }
    assert outcome.faulted.events[2].attributes["headers"]["Authorization"] == (
        "Bearer trace-secret"
    )
    for path in directory.rglob("*.json"):
        contents = path.read_text(encoding="utf-8")
        assert "trace-secret" not in contents
        assert "trace-api-secret" not in contents
        assert "environment-secret" not in contents


def test_preexisting_directory_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FixedDatetime:
        @staticmethod
        def now(_timezone: object) -> datetime:
            return START

    monkeypatch.setattr(writer, "datetime", FixedDatetime)
    monkeypatch.setattr(writer, "uuid4", lambda: SimpleNamespace(hex="collision"))
    directory = tmp_path / "20260101T000000.000000Z-collision"
    directory.mkdir()
    sentinel = directory / "keep.txt"
    sentinel.write_text("do not overwrite", encoding="utf-8")

    with pytest.raises(FileExistsError, match="unique artifact directory"):
        export_experiment(sample_outcome(), root=tmp_path)

    assert sentinel.read_text(encoding="utf-8") == "do not overwrite"
    assert list(tmp_path.iterdir()) == [directory]


def test_failed_write_removes_uncommitted_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = writer._write_json

    def fail_on_faulted(directory: Path, relative: str, data: object) -> None:
        assert not (directory / "manifest.json").exists()
        if relative == "faulted.json":
            raise OSError("disk full")
        original(directory, relative, data)

    monkeypatch.setattr(writer, "_write_json", fail_on_faulted)

    with pytest.raises(OSError, match="disk full"):
        export_experiment(sample_outcome(), root=tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_manifest_commit_failure_does_not_publish_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_link(source: Path, destination: Path) -> None:
        assert source.name == ".manifest.json.tmp"
        assert destination.name == "manifest.json"
        raise OSError("manifest unavailable")

    monkeypatch.setattr(writer.os, "link", fail_link)

    with pytest.raises(OSError, match="manifest unavailable"):
        export_experiment(sample_outcome(), root=tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_inconsistent_report_is_rejected_before_creating_files(tmp_path: Path) -> None:
    report = summarize_repetitions([], requested_repetitions=1)
    object.__setattr__(report, "completed_repetitions", 1)

    with pytest.raises(ValueError, match="inconsistent"):
        export_multi_run(report, experiment=sample_experiment(), root=tmp_path)

    assert list(tmp_path.iterdir()) == []
