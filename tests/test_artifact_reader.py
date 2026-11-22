"""Offline tests for the completed single-run artifact reader."""

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
    export_experiment,
    load_export,
)
from tests.test_artifacts import sample_outcome


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def test_load_and_discover_completed_single_run_export(tmp_path: Path) -> None:
    directory = export_experiment(sample_outcome(), root=tmp_path)
    (tmp_path / "unfinished").mkdir()

    discovered = discover_exports(tmp_path)
    loaded = load_export(directory.name, root=tmp_path)

    assert [manifest.export_id for manifest in discovered] == [directory.name]
    assert loaded.export_id == directory.name
    assert loaded.baseline.run_id == "baseline-1"
    assert loaded.faulted.run_id == "faulted-1"
    assert loaded.comparison.experiment_id == "experiment-1"


def test_legacy_version_one_comparison_uses_model_default(tmp_path: Path) -> None:
    directory = export_experiment(sample_outcome(), root=tmp_path)
    comparison_path = directory / "comparison.json"
    comparison = read_json(comparison_path)
    data = comparison["data"]
    assert isinstance(data, dict)
    data.pop("alternative_tool_calls")
    write_json(comparison_path, comparison)

    loaded = load_export(directory.name, root=tmp_path)

    assert loaded.comparison.alternative_tool_calls == 0


def test_invalid_json_has_clear_format_error(tmp_path: Path) -> None:
    directory = export_experiment(sample_outcome(), root=tmp_path)
    (directory / "baseline.json").write_text("{invalid", encoding="utf-8")

    with pytest.raises(ArtifactFormatError, match="valid JSON"):
        load_export(directory.name, root=tmp_path)


def test_missing_completion_marker_is_not_discovered_or_loadable(
    tmp_path: Path,
) -> None:
    directory = export_experiment(sample_outcome(), root=tmp_path)
    (directory / "manifest.json").unlink()

    assert discover_exports(tmp_path) == []
    with pytest.raises(ArtifactNotFoundError, match="completion manifest"):
        load_export(directory.name, root=tmp_path)


def test_missing_required_file_is_reported(tmp_path: Path) -> None:
    directory = export_experiment(sample_outcome(), root=tmp_path)
    (directory / "faulted.json").unlink()

    with pytest.raises(ArtifactNotFoundError, match="faulted.json"):
        load_export(directory.name, root=tmp_path)


def test_unsupported_schema_version_is_rejected(tmp_path: Path) -> None:
    directory = export_experiment(sample_outcome(), root=tmp_path)
    manifest_path = directory / "manifest.json"
    manifest = read_json(manifest_path)
    manifest["schema_version"] = 2
    write_json(manifest_path, manifest)

    with pytest.raises(UnsupportedArtifactError, match="schema_version 2"):
        load_export(directory.name, root=tmp_path)


def test_wrong_record_type_is_rejected(tmp_path: Path) -> None:
    directory = export_experiment(sample_outcome(), root=tmp_path)
    comparison_path = directory / "comparison.json"
    comparison = read_json(comparison_path)
    comparison["record_type"] = "agent_run"
    write_json(comparison_path, comparison)

    with pytest.raises(UnsupportedArtifactError, match="record_type"):
        load_export(directory.name, root=tmp_path)


def test_export_id_path_traversal_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ArtifactSecurityError, match="invalid export ID"):
        load_export("../outside", root=tmp_path)


def test_export_directory_symlink_cannot_escape_root(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    export_experiment(sample_outcome(), root=outside)
    (root / "linked-export").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactSecurityError, match="escapes configured root"):
        load_export("linked-export", root=root)
    with pytest.raises(ArtifactSecurityError, match="escapes configured root"):
        discover_exports(root)


def test_required_file_symlink_cannot_escape_root(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    directory = export_experiment(sample_outcome(), root=root)
    outside_file = tmp_path / "outside.json"
    shutil.copyfile(directory / "baseline.json", outside_file)
    (directory / "baseline.json").unlink()
    (directory / "baseline.json").symlink_to(outside_file)

    with pytest.raises(ArtifactSecurityError, match="escapes configured root"):
        load_export(directory.name, root=root)
