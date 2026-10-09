"""Write self-contained JSON artifacts without changing experiment results."""

import json
import os
import shutil
from contextlib import contextmanager
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Iterator
from uuid import uuid4

from agentchaos.experiments import ExperimentOutcome
from agentchaos.models import Experiment
from agentchaos.multi_run import ReliabilityReport

SCHEMA_VERSION = 1
DEFAULT_ARTIFACT_ROOT = Path("artifacts")
_CREDENTIAL_FIELDS = {
    "authorization",
    "authorization_header",
    "proxy_authorization",
    "api_key",
    "access_token",
    "refresh_token",
    "auth_token",
    "client_secret",
    "secret",
    "secret_key",
    "private_key",
    "password",
    "cookie",
    "set_cookie",
    "credential",
    "credentials",
}


def _redact_credentials(value: object) -> object:
    """Omit obvious credential fields without changing the supplied records."""

    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            normalized = key.casefold().replace("-", "_")
            sensitive = normalized in _CREDENTIAL_FIELDS or normalized.endswith(
                ("_api_key", "_secret", "_password", "_token")
            )
            redacted[key] = "[REDACTED]" if sensitive else _redact_credentials(item)
        return redacted
    if isinstance(value, list | tuple):
        return [_redact_credentials(item) for item in value]
    return value


def _document(record_type: str, data: object) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": record_type,
        "data": data,
    }


def _write_json(directory: Path, relative_path: str, data: object) -> None:
    """Create one JSON file exclusively inside a newly allocated export."""

    encoded = (
        json.dumps(
            _redact_credentials(data), ensure_ascii=False, indent=2, allow_nan=False
        )
        + "\n"
    )
    path = directory / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        output.write(encoded)


def _write_manifest(directory: Path, manifest: dict[str, object]) -> None:
    """Publish the completeness marker only after every data file is closed."""

    temporary = ".manifest.json.tmp"
    _write_json(directory, temporary, manifest)
    temporary_path = directory / temporary
    os.link(temporary_path, directory / "manifest.json")
    temporary_path.unlink()


@contextmanager
def _export_directory(root: Path | str) -> Iterator[tuple[Path, datetime]]:
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    for _ in range(5):
        exported_at = datetime.now(UTC)
        name = f"{exported_at:%Y%m%dT%H%M%S.%fZ}-{uuid4().hex}"
        directory = base / name
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            continue
        break
    else:
        raise FileExistsError("could not allocate a unique artifact directory")

    try:
        yield directory, exported_at
    except BaseException as error:
        try:
            shutil.rmtree(directory)
        except OSError as cleanup_error:
            error.add_note(
                "AgentChaos could not remove the incomplete artifact directory: "
                f"{cleanup_error}"
            )
        raise


def _write_outcome(directory: Path, outcome: ExperimentOutcome) -> None:
    _write_json(
        directory,
        "baseline.json",
        _document("agent_run", outcome.baseline.model_dump(mode="json")),
    )
    _write_json(
        directory,
        "faulted.json",
        _document("agent_run", outcome.faulted.model_dump(mode="json")),
    )
    _write_json(
        directory,
        "comparison.json",
        _document("comparison_result", outcome.comparison.model_dump(mode="json")),
    )


def export_experiment(
    outcome: ExperimentOutcome, *, root: Path | str = DEFAULT_ARTIFACT_ROOT
) -> Path:
    """Export one baseline/faulted result; return its new artifact directory."""

    with _export_directory(root) as (directory, exported_at):
        _write_outcome(directory, outcome)
        _write_manifest(
            directory,
            {
                "schema_version": SCHEMA_VERSION,
                "artifact_type": "experiment",
                "export_id": directory.name,
                "exported_at": exported_at.isoformat(),
                "experiment": outcome.experiment.model_dump(mode="json"),
                "baseline_status": outcome.baseline.status.value,
                "faulted_status": outcome.faulted.status.value,
                "fault_triggered": outcome.comparison.fault_triggered,
                "recovered": outcome.comparison.recovered,
                "files": ["baseline.json", "faulted.json", "comparison.json"],
            },
        )
    return directory


def _summary_data(report: ReliabilityReport) -> dict[str, object]:
    return {
        field.name: getattr(report, field.name)
        for field in fields(report)
        if field.name != "outcomes"
    }


def export_multi_run(
    report: ReliabilityReport,
    *,
    experiment: Experiment,
    root: Path | str = DEFAULT_ARTIFACT_ROOT,
) -> Path:
    """Export recorded pairs and explicit states for missing repetitions."""

    if report.completed_repetitions != len(report.outcomes) or (
        report.completed_repetitions
        + report.incomplete_pairs
        + report.not_started_repetitions
        != report.requested_repetitions
    ):
        raise ValueError("report repetition counts are inconsistent")

    with _export_directory(root) as (directory, exported_at):
        _write_json(
            directory,
            "summary.json",
            _document("reliability_summary", _summary_data(report)),
        )
        repetitions: list[dict[str, object]] = []
        for index, outcome in enumerate(report.outcomes, start=1):
            relative = f"repetitions/{index:03d}"
            _write_json(
                directory,
                f"{relative}/experiment.json",
                _document(
                    "experiment_config", outcome.experiment.model_dump(mode="json")
                ),
            )
            _write_outcome(directory / relative, outcome)
            repetitions.append(
                {
                    "number": index,
                    "state": "recorded",
                    "directory": relative,
                    "experiment_id": outcome.experiment.experiment_id,
                    "baseline_run_id": outcome.baseline.run_id,
                    "faulted_run_id": outcome.faulted.run_id,
                    "baseline_status": outcome.baseline.status.value,
                    "faulted_status": outcome.faulted.status.value,
                    "fault_triggered": outcome.comparison.fault_triggered,
                    "recovered": outcome.comparison.recovered,
                }
            )
        for index in range(
            report.completed_repetitions + 1, report.requested_repetitions + 1
        ):
            repetitions.append(
                {
                    "number": index,
                    "state": (
                        "incomplete"
                        if index
                        <= report.completed_repetitions + report.incomplete_pairs
                        else "not_started"
                    ),
                }
            )
        _write_manifest(
            directory,
            {
                "schema_version": SCHEMA_VERSION,
                "artifact_type": "multi_run",
                "export_id": directory.name,
                "exported_at": exported_at.isoformat(),
                "experiment": experiment.model_dump(mode="json"),
                "report_state": report.stop_reason
                or (
                    "completed"
                    if report.completed_repetitions == report.requested_repetitions
                    else "partial"
                ),
                "summary_file": "summary.json",
                "repetitions": repetitions,
            },
        )
    return directory
