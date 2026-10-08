"""Collect ordered, normalized events for a single agent run."""

from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import JsonValue

from agentchaos.models import AgentRun, EventKind, RunEvent, RunStatus


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TraceRecorder:
    """Record one run from start through a single terminal outcome.

    Event IDs are unique within the run and derived from its ID and sequence.
    A clock can be supplied for deterministic tests. Sequence is authoritative
    for event order; all timestamps are stored as timezone-aware UTC values.
    """

    def __init__(
        self, run_id: str, *, clock: Callable[[], datetime] | None = None
    ) -> None:
        if not run_id:
            raise ValueError("run_id must not be empty")
        self._run_id = run_id
        self._clock = clock or _utc_now
        self._events: list[RunEvent] = []
        self._started_at: datetime | None = None
        self._task: str | None = None
        self._adapter: str | None = None
        self._finished = False

    @property
    def events(self) -> tuple[RunEvent, ...]:
        """Return a snapshot that cannot mutate recorded events."""

        return tuple(event.model_copy(deep=True) for event in self._events)

    def start(self, *, task: str, adapter: str) -> RunEvent:
        """Begin a run and record ``run.started`` as sequence zero."""

        if self._started_at is not None:
            raise RuntimeError("run has already started")
        if not task or not adapter:
            raise ValueError("task and adapter must not be empty")
        timestamp = self._timestamp()
        event = self._new_event(EventKind.RUN_STARTED, timestamp=timestamp)
        self._task = task
        self._adapter = adapter
        self._started_at = timestamp
        return self._store(event)

    def record_model_request(
        self,
        *,
        call_id: str,
        name: str,
        attributes: dict[str, JsonValue] | None = None,
    ) -> RunEvent:
        """Record the start of a model call."""

        return self._record(
            EventKind.MODEL_REQUEST,
            call_id=call_id,
            name=name,
            attributes=attributes,
        )

    def record_model_response(
        self, *, call_id: str, attributes: dict[str, JsonValue] | None = None
    ) -> RunEvent:
        """Record a successful model response."""

        return self._record(
            EventKind.MODEL_RESPONSE, call_id=call_id, attributes=attributes
        )

    def record_model_error(
        self, *, call_id: str, attributes: dict[str, JsonValue] | None = None
    ) -> RunEvent:
        """Record a failed model call."""

        return self._record(
            EventKind.MODEL_ERROR, call_id=call_id, attributes=attributes
        )

    def record_tool_call(
        self,
        *,
        call_id: str,
        name: str,
        attributes: dict[str, JsonValue] | None = None,
    ) -> RunEvent:
        """Record the start of a tool call."""

        return self._record(
            EventKind.TOOL_CALL, call_id=call_id, name=name, attributes=attributes
        )

    def record_tool_result(
        self, *, call_id: str, attributes: dict[str, JsonValue] | None = None
    ) -> RunEvent:
        """Record a successful tool result."""

        return self._record(
            EventKind.TOOL_RESULT, call_id=call_id, attributes=attributes
        )

    def record_tool_error(
        self, *, call_id: str, attributes: dict[str, JsonValue] | None = None
    ) -> RunEvent:
        """Record a failed tool call."""

        return self._record(
            EventKind.TOOL_ERROR, call_id=call_id, attributes=attributes
        )

    def finish(
        self,
        status: RunStatus,
        *,
        output: JsonValue = None,
        attributes: dict[str, JsonValue] | None = None,
    ) -> AgentRun:
        """Record the terminal event and return a detached, valid ``AgentRun``."""

        self._require_active()
        if status == RunStatus.RUNNING:
            raise ValueError("cannot finish with running status")
        kind = {
            RunStatus.SUCCEEDED: EventKind.RUN_COMPLETED,
            RunStatus.FAILED: EventKind.RUN_FAILED,
            RunStatus.CANCELLED: EventKind.RUN_CANCELLED,
        }[status]
        terminal = self._new_event(kind, attributes=attributes)
        # Validate the complete run before committing the terminal event.
        run = AgentRun(
            run_id=self._run_id,
            task=self._task,
            adapter=self._adapter,
            status=status,
            started_at=self._started_at,
            ended_at=terminal.occurred_at,
            events=[*self.events, terminal.model_copy(deep=True)],
            output=output,
        )
        self._store(terminal)
        self._finished = True
        return run

    def _require_active(self) -> None:
        if self._started_at is None:
            raise RuntimeError("run has not started")
        if self._finished:
            raise RuntimeError("run has already finished")

    def _timestamp(self) -> datetime:
        timestamp = self._clock()
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("clock must return timezone-aware datetimes")
        timestamp = timestamp.astimezone(UTC)
        if self._events and timestamp < self._events[-1].occurred_at:
            raise ValueError("clock moved backward during the run")
        return timestamp

    def _new_event(
        self,
        kind: EventKind,
        *,
        timestamp: datetime | None = None,
        call_id: str | None = None,
        name: str | None = None,
        attributes: dict[str, JsonValue] | None = None,
    ) -> RunEvent:
        sequence = len(self._events)
        return RunEvent(
            event_id=f"{self._run_id}:{sequence}",
            sequence=sequence,
            occurred_at=timestamp or self._timestamp(),
            kind=kind,
            call_id=call_id,
            name=name,
            attributes=attributes or {},
        )

    def _store(self, event: RunEvent) -> RunEvent:
        self._events.append(event.model_copy(deep=True))
        return event.model_copy(deep=True)

    def _record(
        self,
        kind: EventKind,
        *,
        call_id: str,
        name: str | None = None,
        attributes: dict[str, JsonValue] | None = None,
    ) -> RunEvent:
        self._require_active()
        if not call_id:
            raise ValueError("call_id must not be empty")
        return self._store(
            self._new_event(kind, call_id=call_id, name=name, attributes=attributes)
        )
