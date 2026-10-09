"""The documented import surface and developer example remain usable offline."""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agentchaos import (
    AgentRun,
    ComparisonResult,
    EventKind,
    Experiment,
    ExperimentOutcome,
    ExperimentRunner,
    FaultSpec,
    MultiRunRunner,
    ReliabilityReport,
    RunEvent,
    RunStatus,
    TraceRecorder,
    compare_runs,
    required_facts,
)
from agentchaos.adapters import AgentAdapter, FaultCapableAdapter
from agentchaos.adapters.openai_agents import OpenAIAgentsAdapter
from agentchaos.artifacts import export_experiment, export_multi_run
from agentchaos.compare import compare_runs as existing_compare_runs
from agentchaos.compare import required_facts as existing_required_facts
from agentchaos.experiments import ExperimentOutcome as ExistingOutcome
from agentchaos.experiments import ExperimentRunner as ExistingExperimentRunner
from agentchaos.models import AgentRun as ExistingRun
from agentchaos.multi_run import MultiRunRunner as ExistingMultiRunRunner
from agentchaos.tracing import TraceRecorder as ExistingTraceRecorder
from examples.external_integration.__main__ import (
    StockAnswer,
    get_inventory,
    main,
    task_succeeded,
)


def test_public_names_resolve_to_existing_implementations() -> None:
    assert ExperimentRunner is ExistingExperimentRunner
    assert MultiRunRunner is ExistingMultiRunRunner
    assert ExperimentOutcome is ExistingOutcome
    assert TraceRecorder is ExistingTraceRecorder
    assert AgentRun is ExistingRun
    assert compare_runs is existing_compare_runs
    assert required_facts is existing_required_facts
    assert all(
        value is not None
        for value in (
            ComparisonResult,
            EventKind,
            Experiment,
            FaultSpec,
            ReliabilityReport,
            RunEvent,
            RunStatus,
            AgentAdapter,
            FaultCapableAdapter,
            OpenAIAgentsAdapter,
            export_experiment,
            export_multi_run,
        )
    )


def test_core_imports_do_not_load_openai_agents_sdk(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "src"
    script = """
import builtins
original_import = builtins.__import__
def without_agents(name, *args, **kwargs):
    if name == 'agents' or name.startswith('agents.'):
        raise AssertionError('core imported the OpenAI Agents SDK')
    return original_import(name, *args, **kwargs)
builtins.__import__ = without_agents
from agentchaos import Experiment, ExperimentRunner, MultiRunRunner, TraceRecorder
from agentchaos.artifacts import export_experiment
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(source)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_external_example_has_no_import_time_api_requirement() -> None:
    assert get_inventory("SKU-7") == '{"in_stock": true, "product_id": "SKU-7"}'
    assert StockAnswer(product_id="SKU-7", in_stock=True).in_stock is True
    assert main is not None


@pytest.mark.asyncio
async def test_external_example_checks_key_before_live_run(monkeypatch: Any) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="Set OPENAI_API_KEY"):
        await main()


@pytest.mark.asyncio
async def test_external_example_uses_public_runner_without_live_model(
    monkeypatch: Any, capsys: Any
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-placeholder")
    monkeypatch.delenv("AGENTCHAOS_EXPORT", raising=False)
    calls: list[str] = []

    async def fake_sdk_run(agent: Any, task: str, *, hooks: Any, run_config: Any):
        calls.append(task)
        return SimpleNamespace(
            final_output=StockAnswer(product_id="SKU-7", in_stock=True)
        )

    monkeypatch.setattr("agentchaos.adapters.openai_agents.Runner.run", fake_sdk_run)
    await main()
    assert calls == ["Is SKU-7 in stock?", "Is SKU-7 in stock?"]
    assert "Comparison:" in capsys.readouterr().out


def test_external_example_evaluator_uses_structured_values() -> None:
    recorder = TraceRecorder("stock")
    recorder.start(task="Is SKU-7 in stock?", adapter="fake")
    correct = recorder.finish(
        RunStatus.SUCCEEDED,
        output={"product_id": "SKU-7", "in_stock": True},
    )
    assert task_succeeded(correct) is True
    assert (
        task_succeeded(
            correct.model_copy(
                update={"output": {"product_id": "SKU-7", "in_stock": False}}
            )
        )
        is False
    )
    assert task_succeeded(correct.model_copy(update={"output": "malformed"})) is None


@pytest.mark.asyncio
async def test_invalid_adapter_fails_before_any_execution() -> None:
    experiment = Experiment(
        experiment_id="invalid-adapter",
        baseline_run_id="baseline",
        task="Look up A1",
        fault=FaultSpec(
            fault_type="tool_error",
            target_tool="lookup",
            error_message="Unavailable",
        ),
    )
    with pytest.raises(TypeError, match="adapter must provide an async run"):
        await ExperimentRunner().run(adapter=object(), experiment=experiment)
