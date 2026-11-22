"""Read completed single-experiment JSON exports."""

import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    model_validator,
)

from agentchaos.artifacts.writer import DEFAULT_ARTIFACT_ROOT, SCHEMA_VERSION
from agentchaos.experiments import ExperimentOutcome
from agentchaos.models import AgentRun, ComparisonResult, Experiment, RunStatus

_SINGLE_RUN_FILES = ("baseline.json", "faulted.json", "comparison.json")
_JSON_SUMMARY = TypeAdapter(dict[str, JsonValue])


class ArtifactReaderError(ValueError):
    """Base error for an invalid or inaccessible artifact export."""


class ArtifactNotFoundError(ArtifactReaderError, FileNotFoundError):
    """An export or one of its required files does not exist."""


class ArtifactFormatError(ArtifactReaderError):
    """An artifact file is malformed or fails its data model validation."""


class UnsupportedArtifactError(ArtifactReaderError):
    """An artifact uses an unsupported schema or record type."""


class ArtifactTypeMismatchError(UnsupportedArtifactError):
    """The export belongs to another supported artifact reader."""


class ArtifactSecurityError(ArtifactReaderError):
    """An artifact path resolves outside the configured artifact root."""


class ExperimentManifest(BaseModel):
    """Validated completion manifest for one single-run experiment export."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    artifact_type: Literal["experiment"]
    export_id: str
    exported_at: AwareDatetime
    experiment: Experiment
    baseline_status: RunStatus
    faulted_status: RunStatus
    fault_triggered: bool
    recovered: bool | None
    files: list[str]


class ExperimentArtifact(BaseModel):
    """A complete export parsed into AgentChaos domain models."""

    model_config = ConfigDict(frozen=True)

    manifest: ExperimentManifest
    baseline: AgentRun
    faulted: AgentRun
    comparison: ComparisonResult

    @property
    def export_id(self) -> str:
        return self.manifest.export_id

    @property
    def exported_at(self) -> datetime:
        return self.manifest.exported_at


class MultiRunRepetitionManifest(BaseModel):
    """One repetition's explicit state from a multi-run manifest."""

    model_config = ConfigDict(extra="forbid")

    number: int = Field(ge=1)
    state: Literal["recorded", "incomplete", "not_started"]
    directory: str | None = None
    experiment_id: str | None = Field(default=None, min_length=1)
    baseline_run_id: str | None = Field(default=None, min_length=1)
    faulted_run_id: str | None = Field(default=None, min_length=1)
    baseline_status: RunStatus | None = None
    faulted_status: RunStatus | None = None
    fault_triggered: bool | None = None
    recovered: bool | None = None

    @model_validator(mode="after")
    def check_recorded_fields(self) -> "MultiRunRepetitionManifest":
        values = (
            self.directory,
            self.experiment_id,
            self.baseline_run_id,
            self.faulted_run_id,
            self.baseline_status,
            self.faulted_status,
            self.fault_triggered,
        )
        if self.state == "recorded":
            if any(value is None for value in values):
                raise ValueError("recorded repetitions require their run metadata")
            expected_directory = f"repetitions/{self.number:03d}"
            if self.directory != expected_directory:
                raise ValueError(
                    f"recorded repetition directory must be {expected_directory!r}"
                )
        elif any(value is not None for value in values) or self.recovered is not None:
            raise ValueError("unavailable repetitions cannot contain run metadata")
        return self


class MultiRunManifest(BaseModel):
    """Validated completion manifest for a multi-run export."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    artifact_type: Literal["multi_run"]
    export_id: str
    exported_at: AwareDatetime
    experiment: Experiment
    report_state: str
    summary_file: Literal["summary.json"]
    repetitions: list[MultiRunRepetitionManifest]

    @model_validator(mode="after")
    def check_repetition_numbers(self) -> "MultiRunManifest":
        expected = list(range(1, len(self.repetitions) + 1))
        if [item.number for item in self.repetitions] != expected:
            raise ValueError("repetition numbers must be consecutive starting at 1")
        return self


class MultiRunRepetition(BaseModel):
    """A repetition state and its outcome when the writer recorded one."""

    model_config = ConfigDict(frozen=True)

    number: int
    state: Literal["recorded", "incomplete", "not_started"]
    outcome: ExperimentOutcome | None = None


class MultiRunArtifact(BaseModel):
    """A loaded multi-run export with explicit unavailable repetition states."""

    model_config = ConfigDict(frozen=True)

    manifest: MultiRunManifest
    summary: dict[str, JsonValue]
    repetitions: tuple[MultiRunRepetition, ...]

    @property
    def export_id(self) -> str:
        return self.manifest.export_id

    @property
    def exported_at(self) -> datetime:
        return self.manifest.exported_at


def _root_path(root: Path | str) -> Path:
    return Path(root).resolve()


def _contained_path(path: Path, root: Path, *, strict: bool = True) -> Path:
    try:
        resolved = path.resolve(strict=strict)
    except FileNotFoundError:
        raise
    except (OSError, RuntimeError) as exc:
        raise ArtifactReaderError(
            f"cannot safely resolve artifact path: {path}"
        ) from exc
    if not resolved.is_relative_to(root):
        raise ArtifactSecurityError(f"artifact path escapes configured root: {path}")
    return resolved


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ArtifactNotFoundError(
            f"required artifact file is missing: {path}"
        ) from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArtifactFormatError(f"cannot read valid JSON from {path}: {exc}") from exc


def _manifest_document(
    export_dir: Path, export_id: str, root: Path
) -> tuple[Path, dict[str, object]]:
    try:
        path = _contained_path(export_dir / "manifest.json", root)
    except FileNotFoundError as exc:
        raise ArtifactNotFoundError(
            f"artifact export {export_id!r} has no completion manifest"
        ) from exc
    data = _read_json(path)
    if not isinstance(data, dict):
        raise ArtifactFormatError(f"{path} must contain a JSON object")
    version = data.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise UnsupportedArtifactError(
            f"{path} has schema_version {version!r}; supported version is "
            f"{SCHEMA_VERSION}"
        )
    return path, data


def _required_file(export_dir: Path, filename: str, root: Path) -> Path:
    candidate = export_dir / filename
    try:
        path = _contained_path(candidate, root)
    except FileNotFoundError as exc:
        raise ArtifactNotFoundError(
            f"required artifact file is missing: {candidate}"
        ) from exc
    if not path.is_file():
        raise ArtifactNotFoundError(f"required artifact file is missing: {path}")
    return path


def _document_data(path: Path, record_type: str) -> object:
    document = _read_json(path)
    if not isinstance(document, dict):
        raise ArtifactFormatError(f"{path} must contain a JSON object")
    version = document.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise UnsupportedArtifactError(
            f"{path} has schema_version {version!r}; supported version is "
            f"{SCHEMA_VERSION}"
        )
    actual_type = document.get("record_type")
    if actual_type != record_type:
        raise UnsupportedArtifactError(
            f"{path} has record_type {actual_type!r}; expected {record_type!r}"
        )
    if "data" not in document:
        raise ArtifactFormatError(f"{path} is missing its data field")
    return document["data"]


def _validate_model(model_type: type[BaseModel], value: object, path: Path):
    try:
        return model_type.model_validate(value)
    except ValidationError as exc:
        raise ArtifactFormatError(f"invalid data in {path}: {exc}") from exc


def _export_dir(export_id: str, root: Path) -> Path:
    if (
        not export_id
        or export_id in {".", ".."}
        or Path(export_id).name != export_id
        or "/" in export_id
        or "\\" in export_id
    ):
        raise ArtifactSecurityError(f"invalid export ID: {export_id!r}")
    candidate = root / export_id
    try:
        directory = _contained_path(candidate, root)
    except FileNotFoundError as exc:
        raise ArtifactNotFoundError(
            f"artifact export not found: {export_id!r}"
        ) from exc
    if not directory.is_dir():
        raise ArtifactNotFoundError(f"artifact export not found: {export_id!r}")
    return directory


def _manifest(
    export_dir: Path,
    export_id: str,
    root: Path,
    *,
    skip_other_types: bool = False,
) -> ExperimentManifest | None:
    path, data = _manifest_document(export_dir, export_id, root)
    artifact_type = data.get("artifact_type")
    if artifact_type != "experiment":
        if skip_other_types and artifact_type == "multi_run":
            return None
        raise ArtifactTypeMismatchError(
            f"{path} has artifact_type {artifact_type!r}; "
            "only single-run experiment exports are supported"
        )
    try:
        manifest = ExperimentManifest.model_validate(data)
    except ValidationError as exc:
        raise ArtifactFormatError(f"invalid manifest {path}: {exc}") from exc
    if manifest.export_id != export_id:
        raise ArtifactFormatError(
            f"manifest export_id {manifest.export_id!r} does not match directory "
            f"{export_id!r}"
        )
    missing = set(_SINGLE_RUN_FILES) - set(manifest.files)
    if missing:
        raise ArtifactFormatError(
            f"manifest {path} does not list required files: "
            f"{', '.join(sorted(missing))}"
        )
    for name in _SINGLE_RUN_FILES:
        _required_file(export_dir, name, root)
    return manifest


def _export_directories(root: Path) -> list[tuple[str, Path]]:
    """Return directories with a manifest marker, resolving each within root."""

    if not root.exists():
        return []
    if not root.is_dir():
        raise ArtifactReaderError(f"artifact root is not a directory: {root}")
    directories: list[tuple[str, Path]] = []
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        if entry.name.startswith("."):
            continue
        try:
            directory = _contained_path(entry, root)
        except FileNotFoundError:
            continue
        if directory.is_dir() and (directory / "manifest.json").exists():
            directories.append((entry.name, directory))
    return directories


def discover_exports(
    root: Path | str = DEFAULT_ARTIFACT_ROOT,
) -> list[ExperimentManifest]:
    """Return validated completed single-run export manifests under ``root``.

    Directories without ``manifest.json`` are treated as unfinished exports and
    ignored, matching the writer's manifest-last completion marker.
    """

    resolved_root = _root_path(root)
    manifests: list[ExperimentManifest] = []
    for export_id, directory in _export_directories(resolved_root):
        manifest = _manifest(directory, export_id, resolved_root, skip_other_types=True)
        if manifest is not None:
            manifests.append(manifest)
    return manifests


def load_export(
    export_id: str, *, root: Path | str = DEFAULT_ARTIFACT_ROOT
) -> ExperimentArtifact:
    """Load one completed single-run export by its directory/export ID."""

    resolved_root = _root_path(root)
    export_dir = _export_dir(export_id, resolved_root)
    manifest = _manifest(export_dir, export_id, resolved_root)
    assert manifest is not None

    parsed: dict[str, BaseModel] = {}
    for filename, record_type, model_type in (
        ("baseline.json", "agent_run", AgentRun),
        ("faulted.json", "agent_run", AgentRun),
        ("comparison.json", "comparison_result", ComparisonResult),
    ):
        path = _contained_path(export_dir / filename, resolved_root)
        data = _document_data(path, record_type)
        parsed[filename] = _validate_model(model_type, data, path)

    baseline = parsed["baseline.json"]
    faulted = parsed["faulted.json"]
    comparison = parsed["comparison.json"]
    assert isinstance(baseline, AgentRun)
    assert isinstance(faulted, AgentRun)
    assert isinstance(comparison, ComparisonResult)

    expected_ids = (
        (baseline.run_id, manifest.experiment.baseline_run_id, "baseline run"),
        (faulted.run_id, manifest.experiment.faulted_run_id, "faulted run"),
        (comparison.baseline_run_id, baseline.run_id, "comparison baseline"),
        (comparison.faulted_run_id, faulted.run_id, "comparison faulted run"),
        (
            comparison.experiment_id,
            manifest.experiment.experiment_id,
            "comparison experiment",
        ),
    )
    for actual, expected, label in expected_ids:
        if actual != expected:
            raise ArtifactFormatError(
                f"{label} ID {actual!r} does not match expected ID {expected!r}"
            )
    if baseline.status != manifest.baseline_status:
        raise ArtifactFormatError(
            "manifest baseline_status does not match baseline.json"
        )
    if faulted.status != manifest.faulted_status:
        raise ArtifactFormatError("manifest faulted_status does not match faulted.json")
    if comparison.fault_triggered != manifest.fault_triggered:
        raise ArtifactFormatError(
            "manifest fault_triggered does not match comparison.json"
        )
    if comparison.recovered != manifest.recovered:
        raise ArtifactFormatError("manifest recovered does not match comparison.json")
    if baseline.task != faulted.task or (
        manifest.experiment.task is not None
        and baseline.task != manifest.experiment.task
    ):
        raise ArtifactFormatError("run tasks do not match the experiment manifest")
    return ExperimentArtifact(
        manifest=manifest,
        baseline=baseline,
        faulted=faulted,
        comparison=comparison,
    )


def _multi_run_manifest(
    export_dir: Path,
    export_id: str,
    root: Path,
    *,
    skip_other_types: bool = False,
) -> MultiRunManifest | None:
    path, data = _manifest_document(export_dir, export_id, root)
    if data.get("artifact_type") != "multi_run":
        if skip_other_types and data.get("artifact_type") == "experiment":
            return None
        raise ArtifactTypeMismatchError(
            f"{path} has artifact_type {data.get('artifact_type')!r}; "
            "expected 'multi_run'"
        )
    try:
        manifest = MultiRunManifest.model_validate(data)
    except ValidationError as exc:
        raise ArtifactFormatError(f"invalid multi-run manifest {path}: {exc}") from exc
    if manifest.export_id != export_id:
        raise ArtifactFormatError(
            f"manifest export_id {manifest.export_id!r} does not match directory "
            f"{export_id!r}"
        )
    _required_file(export_dir, manifest.summary_file, root)
    for repetition in manifest.repetitions:
        if repetition.state != "recorded":
            continue
        assert repetition.directory is not None
        try:
            repetition_dir = _contained_path(export_dir / repetition.directory, root)
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError(
                f"recorded repetition directory is missing: {repetition.directory}"
            ) from exc
        if not repetition_dir.is_dir():
            raise ArtifactNotFoundError(
                f"recorded repetition directory is missing: {repetition.directory}"
            )
        for filename in (
            "experiment.json",
            "baseline.json",
            "faulted.json",
            "comparison.json",
        ):
            _required_file(repetition_dir, filename, root)
    return manifest


def discover_multi_run_exports(
    root: Path | str = DEFAULT_ARTIFACT_ROOT,
) -> list[MultiRunManifest]:
    """Return validated completed multi-run export manifests under ``root``."""

    resolved_root = _root_path(root)
    manifests: list[MultiRunManifest] = []
    for export_id, directory in _export_directories(resolved_root):
        manifest = _multi_run_manifest(
            directory, export_id, resolved_root, skip_other_types=True
        )
        if manifest is not None:
            manifests.append(manifest)
    return manifests


def load_multi_run_export(
    export_id: str, *, root: Path | str = DEFAULT_ARTIFACT_ROOT
) -> MultiRunArtifact:
    """Load a multi-run export, preserving unavailable repetition states."""

    resolved_root = _root_path(root)
    export_dir = _export_dir(export_id, resolved_root)
    manifest = _multi_run_manifest(export_dir, export_id, resolved_root)
    assert manifest is not None

    summary_path = _required_file(export_dir, manifest.summary_file, resolved_root)
    summary_data = _document_data(summary_path, "reliability_summary")
    if not isinstance(summary_data, dict):
        raise ArtifactFormatError(f"{summary_path} data must be a JSON object")
    try:
        summary = _JSON_SUMMARY.validate_python(summary_data)
    except ValidationError as exc:
        raise ArtifactFormatError(
            f"invalid summary data in {summary_path}: {exc}"
        ) from exc

    recorded_count = sum(item.state == "recorded" for item in manifest.repetitions)
    incomplete_count = sum(item.state == "incomplete" for item in manifest.repetitions)
    not_started_count = sum(
        item.state == "not_started" for item in manifest.repetitions
    )
    counts = {
        "requested_repetitions": len(manifest.repetitions),
        "completed_repetitions": recorded_count,
        "incomplete_pairs": incomplete_count,
        "not_started_repetitions": not_started_count,
        "triggered_faults": sum(
            item.fault_triggered is True
            for item in manifest.repetitions
            if item.state == "recorded"
        ),
        "failed_pairs": sum(
            item.baseline_status != RunStatus.SUCCEEDED
            or item.faulted_status != RunStatus.SUCCEEDED
            for item in manifest.repetitions
            if item.state == "recorded"
        ),
    }
    for field, expected in counts.items():
        actual = summary.get(field)
        if type(actual) is not int or actual != expected:
            raise ArtifactFormatError(
                f"summary {field} is {actual!r}; manifest implies {expected}"
            )

    repetitions: list[MultiRunRepetition] = []
    for item in manifest.repetitions:
        if item.state != "recorded":
            repetitions.append(
                MultiRunRepetition(number=item.number, state=item.state, outcome=None)
            )
            continue

        assert item.directory is not None
        repetition_dir = _contained_path(export_dir / item.directory, resolved_root)
        experiment_path = _required_file(
            repetition_dir, "experiment.json", resolved_root
        )
        experiment_data = _document_data(experiment_path, "experiment_config")
        experiment = _validate_model(Experiment, experiment_data, experiment_path)
        baseline_path = _required_file(repetition_dir, "baseline.json", resolved_root)
        baseline_data = _document_data(baseline_path, "agent_run")
        baseline = _validate_model(AgentRun, baseline_data, baseline_path)
        faulted_path = _required_file(repetition_dir, "faulted.json", resolved_root)
        faulted_data = _document_data(faulted_path, "agent_run")
        faulted = _validate_model(AgentRun, faulted_data, faulted_path)
        comparison_path = _required_file(
            repetition_dir, "comparison.json", resolved_root
        )
        comparison_data = _document_data(comparison_path, "comparison_result")
        comparison = _validate_model(ComparisonResult, comparison_data, comparison_path)
        assert isinstance(experiment, Experiment)
        assert isinstance(baseline, AgentRun)
        assert isinstance(faulted, AgentRun)
        assert isinstance(comparison, ComparisonResult)

        expected_ids = (
            (experiment.experiment_id, item.experiment_id, "experiment"),
            (baseline.run_id, item.baseline_run_id, "baseline run"),
            (faulted.run_id, item.faulted_run_id, "faulted run"),
            (baseline.run_id, experiment.baseline_run_id, "experiment baseline"),
            (faulted.run_id, experiment.faulted_run_id, "experiment faulted run"),
            (
                comparison.experiment_id,
                experiment.experiment_id,
                "comparison experiment",
            ),
            (comparison.baseline_run_id, baseline.run_id, "comparison baseline"),
            (comparison.faulted_run_id, faulted.run_id, "comparison faulted run"),
        )
        for actual, expected, label in expected_ids:
            if actual != expected:
                raise ArtifactFormatError(
                    f"repetition {item.number} {label} ID {actual!r} "
                    f"does not match expected ID {expected!r}"
                )
        if experiment.task != manifest.experiment.task or (
            experiment.fault != manifest.experiment.fault
        ):
            raise ArtifactFormatError(
                f"repetition {item.number} experiment does not match multi-run config"
            )
        if baseline.task != faulted.task or baseline.task != experiment.task:
            raise ArtifactFormatError(
                f"repetition {item.number} run tasks do not match its experiment"
            )
        if baseline.status != item.baseline_status:
            raise ArtifactFormatError(
                f"repetition {item.number} baseline status does not match manifest"
            )
        if faulted.status != item.faulted_status:
            raise ArtifactFormatError(
                f"repetition {item.number} faulted status does not match manifest"
            )
        if comparison.fault_triggered != item.fault_triggered:
            raise ArtifactFormatError(
                f"repetition {item.number} fault_triggered does not match manifest"
            )
        if comparison.recovered != item.recovered:
            raise ArtifactFormatError(
                f"repetition {item.number} recovered does not match manifest"
            )

        repetitions.append(
            MultiRunRepetition(
                number=item.number,
                state="recorded",
                outcome=ExperimentOutcome(
                    experiment=experiment,
                    baseline=baseline,
                    faulted=faulted,
                    comparison=comparison,
                ),
            )
        )

    expected_report_state = summary.get("stop_reason") or (
        "completed" if recorded_count == len(manifest.repetitions) else "partial"
    )
    if expected_report_state != manifest.report_state:
        raise ArtifactFormatError(
            "manifest report_state does not match summary and repetition states"
        )

    return MultiRunArtifact(
        manifest=manifest,
        summary=summary,
        repetitions=tuple(repetitions),
    )
