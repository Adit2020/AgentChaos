"""Small local CLI around the public experiment, evaluation, and export APIs."""

import asyncio
import importlib
import inspect
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from agentchaos import (
    AgentRun,
    Experiment,
    ExperimentOutcome,
    ExperimentRunner,
    MultiRunRunner,
    ReliabilityReport,
    required_facts,
)
from agentchaos.adapters import FaultCapableAdapter
from agentchaos.artifacts import export_experiment, export_multi_run
from agentchaos.cli_config import (
    ConfigurationError,
    ExactEvaluationConfig,
    ExperimentConfig,
    FactsEvaluationConfig,
    load_config,
)

EXIT_INVALID_CONFIG = 2
EXIT_ADAPTER_ERROR = 3
EXIT_RUNTIME_ERROR = 4

app = typer.Typer(
    help="Run local AgentChaos experiments from trusted JSON configurations.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()
error_console = Console(stderr=True)


class AdapterLoadError(RuntimeError):
    """The configured developer adapter could not be constructed safely."""


def _fail(message: str, code: int) -> None:
    error_console.print(Text(f"Error: {message}"))
    raise typer.Exit(code)


def _load_adapter(reference: str) -> FaultCapableAdapter:
    """Import and call one trusted synchronous factory, then check its contract."""

    module_name, factory_name = reference.split(":", maxsplit=1)
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise AdapterLoadError(
            f"cannot import {module_name!r}: {type(exc).__name__}: {exc}"
        ) from exc
    factory = getattr(module, factory_name, None)
    if not callable(factory):
        raise AdapterLoadError(
            f"{reference!r} does not name a callable adapter factory"
        )
    if inspect.iscoroutinefunction(factory):
        raise AdapterLoadError(
            "adapter factory must be synchronous in config version 1"
        )
    try:
        adapter = factory()
    except Exception as exc:
        raise AdapterLoadError(
            f"adapter factory {reference!r} failed: {type(exc).__name__}: {exc}"
        ) from exc
    if inspect.isawaitable(adapter):
        close = getattr(adapter, "close", None)
        if callable(close):
            close()
        raise AdapterLoadError("adapter factory must return an adapter synchronously")
    run = getattr(adapter, "run", None)
    if not callable(run) or not inspect.iscoroutinefunction(run):
        raise AdapterLoadError(
            "adapter must provide async run(task, *, recorder, fault)"
        )
    try:
        inspect.signature(run).bind("task", recorder=object(), fault=None)
    except (TypeError, ValueError) as exc:
        raise AdapterLoadError(
            "adapter.run must accept task, recorder, and fault"
        ) from exc
    return adapter


def _evaluation_kwargs(config: ExperimentConfig) -> dict[str, Any]:
    evaluation = config.evaluation
    if isinstance(evaluation, ExactEvaluationConfig):
        return {"expected_output": evaluation.expected_output}
    if isinstance(evaluation, FactsEvaluationConfig):
        facts = evaluation.facts
        insensitive = tuple(evaluation.case_insensitive_fields)

        def evaluate(run: AgentRun) -> bool | None:
            return required_facts(run, facts, case_insensitive_fields=insensitive)

        return {"evaluator": evaluate}
    raise AssertionError("validated configuration has an unsupported evaluation")


def _verdict(value: bool | None) -> str:
    return "unknown" if value is None else "yes" if value else "no"


def _rate(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.1%}"


def _show_comparison(outcome: ExperimentOutcome, *, title: str = "Comparison") -> None:
    result = outcome.comparison
    table = Table(title=title)
    table.add_column("Metric")
    table.add_column("Value")
    rows = [
        ("Baseline status", outcome.baseline.status.value),
        ("Faulted status", outcome.faulted.status.value),
        ("Baseline task success", _verdict(result.baseline_task_success)),
        ("Faulted task success", _verdict(result.faulted_task_success)),
        ("Fault triggered", _verdict(result.fault_triggered)),
        ("Continued after fault", _verdict(result.continued_after_fault)),
        ("Target tool retries", str(result.target_tool_retries)),
        ("Completed alternative tool calls", str(result.alternative_tool_calls)),
        ("Recovered", _verdict(result.recovered)),
        ("Extra model calls", f"{result.model_call_delta:+d}"),
        ("Extra tool calls", f"{result.tool_call_delta:+d}"),
        (
            "Duration delta (ms)",
            "unknown"
            if result.duration_delta_ms is None
            else f"{result.duration_delta_ms:+d}",
        ),
    ]
    for metric, value in rows:
        table.add_row(metric, value)
    console.print(table)


def _show_report(report: ReliabilityReport) -> None:
    summary = Table(title="Reliability summary")
    summary.add_column("Metric")
    summary.add_column("Value")
    for metric, value in [
        ("Requested repetitions", str(report.requested_repetitions)),
        ("Completed pairs", str(report.completed_repetitions)),
        ("Incomplete pairs", str(report.incomplete_pairs)),
        ("Not started", str(report.not_started_repetitions)),
        ("Triggered faults", str(report.triggered_faults)),
        ("Fault trigger rate", _rate(report.fault_trigger_rate)),
        ("Confirmed recoveries", str(report.confirmed_recoveries)),
        ("Confirmed nonrecoveries", str(report.confirmed_nonrecoveries)),
        ("Unknown recoveries", str(report.unknown_recoveries)),
        ("Confirmed recovery rate", _rate(report.confirmed_recovery_rate)),
        ("Baseline task success rate", _rate(report.baseline_task_success_rate)),
        ("Faulted task success rate", _rate(report.faulted_task_success_rate)),
    ]:
        summary.add_row(metric, value)
    console.print(summary)

    individual = Table(title="Recorded repetitions")
    for name in (
        "#",
        "Baseline",
        "Faulted",
        "Fault triggered",
        "Task success",
        "Recovered",
    ):
        individual.add_column(name)
    for index, outcome in enumerate(report.outcomes, start=1):
        result = outcome.comparison
        individual.add_row(
            str(index),
            outcome.baseline.status.value,
            outcome.faulted.status.value,
            _verdict(result.fault_triggered),
            _verdict(result.faulted_task_success),
            _verdict(result.recovered),
        )
    console.print(individual)


def _runtime_failure(exc: Exception) -> None:
    error_console.print(Text(f"Experiment failed: {type(exc).__name__}: {exc}"))
    for note in getattr(exc, "__notes__", ()):
        error_console.print(Text(note))
    raise typer.Exit(EXIT_RUNTIME_ERROR) from exc


@app.command()
def validate(
    config_file: Annotated[Path, typer.Argument(help="JSON config file")],
) -> None:
    """Validate configuration data without importing the developer adapter."""

    try:
        config = load_config(config_file)
    except ConfigurationError as exc:
        _fail(str(exc), EXIT_INVALID_CONFIG)
    console.print(
        f"Valid configuration (schema version {config.schema_version}). "
        "Adapter availability was not checked."
    )


@app.command("run")
def run_experiment(
    config_file: Annotated[Path, typer.Argument(help="JSON config file")],
    export: Annotated[
        bool, typer.Option("--export", help="Write JSON artifacts")
    ] = False,
    repetitions: Annotated[
        int | None,
        typer.Option("--repetitions", help="Sequential baseline/faulted pairs (1–20)"),
    ] = None,
) -> None:
    """Run one experiment or a bounded sequential batch."""

    try:
        config = load_config(config_file)
    except ConfigurationError as exc:
        _fail(str(exc), EXIT_INVALID_CONFIG)

    multi_runner = None
    if repetitions is not None:
        try:
            multi_runner = MultiRunRunner(max_repetitions=20)
            planned = multi_runner.planned_agent_executions(repetitions)
        except ValueError as exc:
            _fail(str(exc), EXIT_INVALID_CONFIG)
        console.print(
            f"Planned: {repetitions} baseline + {repetitions} faulted "
            f"= {planned} agent executions."
        )

    experiment = Experiment(
        experiment_id=uuid4().hex,
        baseline_run_id=uuid4().hex,
        task=config.task,
        fault=config.fault.to_fault_spec(),
    )
    try:
        adapter = _load_adapter(config.adapter_factory)
    except AdapterLoadError as exc:
        _fail(str(exc), EXIT_ADAPTER_ERROR)

    kwargs = _evaluation_kwargs(config)
    if multi_runner is None:
        try:
            outcome = asyncio.run(
                ExperimentRunner().run(adapter=adapter, experiment=experiment, **kwargs)
            )
            _show_comparison(outcome)
            if export:
                console.print(f"Artifacts: {export_experiment(outcome).resolve()}")
        except Exception as exc:
            partial = getattr(exc, "agentchaos_experiment", None)
            if isinstance(partial, ExperimentOutcome):
                _show_comparison(partial, title="Partial comparison")
            _runtime_failure(exc)
        return

    try:
        report = asyncio.run(
            multi_runner.run(
                adapter=adapter,
                experiment=experiment,
                repetitions=repetitions,
                **kwargs,
            )
        )
        _show_report(report)
        if export:
            path = export_multi_run(report, experiment=experiment)
            console.print(f"Artifacts: {path.resolve()}")
    except Exception as exc:
        partial = getattr(exc, "agentchaos_multi_run_report", None)
        if isinstance(partial, ReliabilityReport):
            _show_report(partial)
        _runtime_failure(exc)
