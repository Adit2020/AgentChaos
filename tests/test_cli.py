"""Offline CLI behavior with trusted local fake adapters."""

import copy
import json
from pathlib import Path
from typing import Any

import agentchaos_example_external
import pytest
from typer.testing import CliRunner

from agentchaos.cli import app
from agentchaos.cli_config import CONFIG_SCHEMA_VERSION, load_config
from tests.cli_fake_adapter import LocalAdapter
from tests.test_artifacts import sample_outcome

runner = CliRunner()
BASE_CONFIG = {
    "schema_version": CONFIG_SCHEMA_VERSION,
    "task": "Look up A1",
    "adapter_factory": "tests.cli_fake_adapter:create_adapter",
    "fault": {
        "fault_type": "tool_error",
        "target_tool": "lookup",
        "occurrence": 1,
        "error_message": "Temporary failure",
    },
    "evaluation": {"type": "exact", "expected_output": {"status": "ready"}},
}


def write_config(tmp_path: Path, data: object = BASE_CONFIG) -> Path:
    path = tmp_path / "experiment.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "validate" in result.output
    assert "run" in result.output


def test_validate_checks_data_without_importing_adapter_or_writing(
    tmp_path: Path,
) -> None:
    config = copy.deepcopy(BASE_CONFIG)
    config["adapter_factory"] = "not_installed_anywhere:create_adapter"
    path = write_config(tmp_path, config)

    result = runner.invoke(app, ["validate", str(path)])

    assert result.exit_code == 0
    assert "Adapter availability was not checked" in result.output
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"schema_version": 2}, "schema_version"),
        ({"fault": {**BASE_CONFIG["fault"], "fault_type": "unknown"}}, "fault_type"),
        (
            {"fault": {**BASE_CONFIG["fault"], "target_tool": ""}},
            "target_tool",
        ),
        (
            {
                "fault": {
                    "fault_type": "tool_timeout",
                    "target_tool": "lookup",
                    "parameters": {"timeout_ms": 251},
                }
            },
            "timeout_ms",
        ),
        (
            {
                "fault": {
                    "fault_type": "malformed_tool_output",
                    "target_tool": "lookup",
                    "parameters": {"payload": ""},
                }
            },
            "payload",
        ),
        (
            {"fault": {**BASE_CONFIG["fault"], "parameters": {"surprise": 1}}},
            "unsupported parameters",
        ),
        ({"adapter_factory": "not-a-module"}, "adapter_factory"),
        ({"evaluation": {"type": "required_facts", "facts": {}}}, "facts"),
    ],
)
def test_invalid_configuration_is_actionable(
    tmp_path: Path, change: dict[str, Any], message: str
) -> None:
    path = write_config(tmp_path, {**copy.deepcopy(BASE_CONFIG), **change})
    result = runner.invoke(app, ["validate", str(path)])
    assert result.exit_code == 2
    assert message in result.output


def test_missing_file_and_malformed_json_are_configuration_errors(
    tmp_path: Path,
) -> None:
    missing = runner.invoke(app, ["validate", str(tmp_path / "missing.json")])
    assert missing.exit_code == 2
    assert "cannot read" in missing.output
    path = tmp_path / "broken.json"
    path.write_text("{not-json", encoding="utf-8")
    malformed = runner.invoke(app, ["validate", str(path)])
    assert malformed.exit_code == 2
    assert "invalid JSON" in malformed.output


@pytest.mark.parametrize(
    "fault",
    [
        BASE_CONFIG["fault"],
        {
            "fault_type": "tool_timeout",
            "target_tool": "lookup",
            "parameters": {"timeout_ms": 10},
        },
        {
            "fault_type": "malformed_tool_output",
            "target_tool": "lookup",
            "parameters": {"payload": "{bad-json"},
        },
    ],
)
def test_all_supported_fault_configs_validate(tmp_path: Path, fault: dict) -> None:
    path = write_config(tmp_path, {**BASE_CONFIG, "fault": fault})
    assert runner.invoke(app, ["validate", str(path)]).exit_code == 0


def test_facts_evaluation_config_is_validated_without_execution(tmp_path: Path) -> None:
    config = {
        **BASE_CONFIG,
        "evaluation": {
            "type": "required_facts",
            "facts": {"status": "ready"},
            "case_insensitive_fields": ["status"],
        },
    }
    path = write_config(tmp_path, config)
    assert load_config(path).evaluation.type == "required_facts"
    assert runner.invoke(app, ["validate", str(path)]).exit_code == 0


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        ("missing_cli_module:create_adapter", "cannot import"),
        ("tests.cli_fake_adapter:not_present", "does not name a callable"),
        ("tests.cli_fake_adapter:incompatible_adapter", "adapter must provide async"),
        ("tests.cli_fake_adapter:async_factory", "must be synchronous"),
        ("tests.cli_fake_adapter:raising_factory", "factory initialization failed"),
    ],
)
def test_adapter_loading_failures_exit_three(
    tmp_path: Path, monkeypatch: Any, factory: str, message: str
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_config(tmp_path, {**BASE_CONFIG, "adapter_factory": factory})
    result = runner.invoke(app, ["run", str(path)])
    assert result.exit_code == 3
    assert message in result.output
    assert not (tmp_path / "artifacts").exists()


def test_single_run_uses_existing_runner_and_reports_comparison(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_config(tmp_path)
    result = runner.invoke(app, ["run", str(path)])
    assert result.exit_code == 0, result.output
    assert "Comparison" in result.output
    assert "Fault triggered" in result.output
    assert "Recovered" in result.output
    assert not (tmp_path / "artifacts").exists()


def test_bundled_sample_reaches_installed_factory_without_model_run(
    tmp_path: Path, monkeypatch: Any
) -> None:
    sample = (
        Path(__file__).resolve().parents[1]
        / "examples/external_integration/experiment.json"
    )
    monkeypatch.chdir(tmp_path)
    created: list[LocalAdapter] = []
    received: list[LocalAdapter] = []

    def fake_factory() -> LocalAdapter:
        adapter = LocalAdapter()
        created.append(adapter)
        return adapter

    async def fake_run(self: Any, *, adapter: LocalAdapter, **kwargs: Any) -> Any:
        received.append(adapter)
        return sample_outcome()

    monkeypatch.setattr(agentchaos_example_external, "create_adapter", fake_factory)
    monkeypatch.setattr("agentchaos.cli.ExperimentRunner.run", fake_run)

    result = runner.invoke(app, ["run", str(sample)])

    assert result.exit_code == 0, result.output
    assert len(created) == 1
    assert received == created
    assert "Comparison" in result.output


def test_multi_run_is_sequential_and_reports_planned_pairs(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_config(tmp_path)
    result = runner.invoke(app, ["run", str(path), "--repetitions", "3"])
    assert result.exit_code == 0, result.output
    assert "3 baseline + 3 faulted = 6 agent executions" in result.output
    assert "Reliability summary" in result.output
    assert "Recorded repetitions" in result.output
    assert not (tmp_path / "artifacts").exists()


@pytest.mark.parametrize("count", ["0", "21"])
def test_repetition_cap_is_configuration_error(
    tmp_path: Path, monkeypatch: Any, count: str
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_config(tmp_path)
    result = runner.invoke(app, ["run", str(path), "--repetitions", count])
    assert result.exit_code == 2
    assert "repetitions must be" in result.output


@pytest.mark.parametrize("repetitions", [None, "2"])
def test_export_is_optional_and_uses_versioned_artifacts(
    tmp_path: Path, monkeypatch: Any, repetitions: str | None
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_config(tmp_path)
    args = ["run", str(path), "--export"]
    if repetitions is not None:
        args.extend(["--repetitions", repetitions])
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "Artifacts:" in result.output
    directories = list((tmp_path / "artifacts").iterdir())
    assert len(directories) == 1
    manifest = json.loads((directories[0] / "manifest.json").read_text())
    assert manifest["artifact_type"] == (
        "experiment" if repetitions is None else "multi_run"
    )


def test_completed_wrong_answer_still_exits_zero(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_config(
        tmp_path,
        {
            **BASE_CONFIG,
            "adapter_factory": "tests.cli_fake_adapter:incorrect_adapter",
        },
    )
    result = runner.invoke(app, ["run", str(path)])
    assert result.exit_code == 0
    assert "Faulted task success" in result.output


def test_runtime_failure_exits_four(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    path = write_config(
        tmp_path,
        {**BASE_CONFIG, "adapter_factory": "tests.cli_fake_adapter:failing_adapter"},
    )
    result = runner.invoke(app, ["run", str(path)])
    assert result.exit_code == 4
    assert "local adapter stopped" in result.output
