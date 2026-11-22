"""Offline tests for completed multi-run artifact reading."""

import json
import shutil
from pathlib import Path

import pytest

from agentchaos.artifacts import (
    ArtifactFormatError,
    ArtifactNotFoundError,
    ArtifactSecurityError,
    UnsupportedArtifactError,
    discover_exports,
    discover_multi_run_exports,
    export_multi_run,
    load_multi_run_export,
)
from agentchaos.multi_run import summarize_repetitions
from tests.test_artifacts import sample_experiment, sample_outcome


def multi_export(
    root: Path,
    *,
    outcomes: tuple = (sample_outcome(1), sample_outcome(2)),
    requested: int = 4,
    incomplete: int = 1,
    stop_reason: str | None = "error",
) -> Path:
    report = summarize_repetitions(
        outcomes,
        requested_repetitions=requested,
        incomplete_pairs=incomplete,
        stop_reason=stop_reason,
    )
    return export_multi_run(report, experiment=sample_experiment(), root=root)


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_discover_and_load_multi_run_with_all_manifest_states(tmp_path: Path) -> None:
    directory = multi_export(tmp_path)

    discovered = discover_multi_run_exports(tmp_path)
    loaded = load_multi_run_export(directory.name, root=tmp_path)

    assert [manifest.export_id for manifest in discovered] == [directory.name]
    assert loaded.export_id == directory.name
    assert [repetition.state for repetition in loaded.repetitions] == [
        "recorded",
        "recorded",
        "incomplete",
        "not_started",
    ]
    assert [repetition.outcome is not None for repetition in loaded.repetitions] == [
        True,
        True,
        False,
        False,
    ]
    assert loaded.repetitions[0].outcome.experiment.experiment_id == "experiment-1"
    assert loaded.summary["completed_repetitions"] == 2
    assert discover_exports(tmp_path) == []


def test_all_unstarted_multi_run_has_explicit_empty_outcomes(tmp_path: Path) -> None:
    directory = multi_export(
        tmp_path, outcomes=(), requested=3, incomplete=0, stop_reason="requested"
    )

    loaded = load_multi_run_export(directory.name, root=tmp_path)

    assert [(item.number, item.state, item.outcome) for item in loaded.repetitions] == [
        (1, "not_started", None),
        (2, "not_started", None),
        (3, "not_started", None),
    ]


def test_missing_summary_is_reported(tmp_path: Path) -> None:
    directory = multi_export(tmp_path)
    (directory / "summary.json").unlink()

    with pytest.raises(ArtifactNotFoundError, match="summary.json"):
        load_multi_run_export(directory.name, root=tmp_path)


def test_missing_recorded_repetition_document_is_reported(tmp_path: Path) -> None:
    directory = multi_export(tmp_path)
    (directory / "repetitions" / "001" / "faulted.json").unlink()

    with pytest.raises(ArtifactNotFoundError, match="faulted.json"):
        load_multi_run_export(directory.name, root=tmp_path)


def test_malformed_repetition_json_is_reported(tmp_path: Path) -> None:
    directory = multi_export(tmp_path)
    (directory / "repetitions" / "001" / "experiment.json").write_text(
        "not-json", encoding="utf-8"
    )

    with pytest.raises(ArtifactFormatError, match="valid JSON"):
        load_multi_run_export(directory.name, root=tmp_path)


def test_unsupported_summary_schema_is_rejected(tmp_path: Path) -> None:
    directory = multi_export(tmp_path)
    path = directory / "summary.json"
    data = read_json(path)
    data["schema_version"] = 2
    write_json(path, data)

    with pytest.raises(UnsupportedArtifactError, match="schema_version 2"):
        load_multi_run_export(directory.name, root=tmp_path)


def test_inconsistent_repetition_ids_are_rejected(tmp_path: Path) -> None:
    directory = multi_export(tmp_path)
    path = directory / "manifest.json"
    manifest = read_json(path)
    manifest["repetitions"][0]["baseline_run_id"] = "wrong-run-id"
    write_json(path, manifest)

    with pytest.raises(ArtifactFormatError, match="baseline run ID"):
        load_multi_run_export(directory.name, root=tmp_path)


def test_summary_counts_must_match_manifest_states(tmp_path: Path) -> None:
    directory = multi_export(tmp_path)
    path = directory / "summary.json"
    summary = read_json(path)
    summary["data"]["incomplete_pairs"] = 0
    write_json(path, summary)

    with pytest.raises(ArtifactFormatError, match="incomplete_pairs"):
        load_multi_run_export(directory.name, root=tmp_path)


def test_manifest_repetition_path_traversal_is_rejected(tmp_path: Path) -> None:
    directory = multi_export(tmp_path)
    path = directory / "manifest.json"
    manifest = read_json(path)
    manifest["repetitions"][0]["directory"] = "repetitions/../../outside"
    write_json(path, manifest)

    with pytest.raises(ArtifactFormatError, match="directory must be"):
        load_multi_run_export(directory.name, root=tmp_path)


def test_repetition_directory_symlink_cannot_escape_root(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    directory = multi_export(root)
    repetition_dir = directory / "repetitions" / "001"
    outside = tmp_path / "outside-repetition"
    shutil.copytree(repetition_dir, outside)
    shutil.rmtree(repetition_dir)
    repetition_dir.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactSecurityError, match="escapes configured root"):
        load_multi_run_export(directory.name, root=root)
    with pytest.raises(ArtifactSecurityError, match="escapes configured root"):
        discover_multi_run_exports(root)


def test_repetition_file_symlink_cannot_escape_root(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    directory = multi_export(root)
    repetition_dir = directory / "repetitions" / "001"
    outside = tmp_path / "outside-baseline.json"
    shutil.copyfile(repetition_dir / "baseline.json", outside)
    (repetition_dir / "baseline.json").unlink()
    (repetition_dir / "baseline.json").symlink_to(outside)

    with pytest.raises(ArtifactSecurityError, match="escapes configured root"):
        load_multi_run_export(directory.name, root=root)
