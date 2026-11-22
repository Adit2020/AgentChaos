import { useEffect, useId, useMemo, useState } from 'react';
import { Check, ChevronDown, Circle, MessageSquare, TriangleAlert, Wrench } from 'lucide-react';
import type { AgentRun, RunEvent } from '../api/contracts';
import { eventPresentation, milliseconds, signed, utcTime } from '../lib/presentation';
import { Badge } from './ui';

function EventIcon({ event }: { event: RunEvent }) {
  if (event.attributes.injected === true || event.kind.endsWith('error'))
    return <TriangleAlert size={15} />;
  if (event.kind.startsWith('model')) return <MessageSquare size={15} />;
  if (event.kind === 'tool.call') return <Wrench size={15} />;
  if (event.kind === 'tool.result' || event.kind === 'run.completed') return <Check size={15} />;
  return <Circle size={13} />;
}

export function EventTimeline({
  run,
  title,
  duration,
  focusRequest,
}: {
  run: AgentRun;
  title: string;
  duration: number | null;
  focusRequest?: { eventId: string; token: number } | null;
}) {
  const panelId = useId();
  const events = useMemo(
    () => [...run.events].sort((a, b) => a.sequence - b.sequence),
    [run.events],
  );
  const [selected, setSelected] = useState<string | null>(
    () => run.events.find((event) => event.attributes.injected === true)?.event_id ?? null,
  );
  const selectedEvent = events.find((event) => event.event_id === selected);
  useEffect(() => {
    if (!focusRequest) return;
    const target = events.find((event) => event.event_id === focusRequest.eventId);
    if (!target) return;
    setSelected(target.event_id);
    document
      .getElementById(`${panelId}-event-${target.sequence}`)
      ?.scrollIntoView({ block: 'center' });
  }, [events, focusRequest, panelId]);
  const byCall = useMemo(() => {
    const index = new Map<string, RunEvent[]>();
    for (const event of events)
      if (event.call_id) index.set(event.call_id, [...(index.get(event.call_id) ?? []), event]);
    return index;
  }, [events]);
  const statusTone =
    run.status === 'succeeded' ? 'success' : run.status === 'failed' ? 'danger' : 'neutral';
  return (
    <section className="trace-panel" aria-label={`${title} execution trace`}>
      <header className="trace-header">
        <div>
          <h3>{title}</h3>
          <Badge tone={statusTone}>{run.status}</Badge>
        </div>
        <span className="mono">{milliseconds(duration)}</span>
      </header>
      <div className="trace-caption">
        <span>{events.length} events · sequence order</span>
        <span>Timestamps in UTC</span>
      </div>
      <div className="trace-events">
        {events.length === 0 ? (
          <p className="no-events">No events recorded.</p>
        ) : (
          events.map((event) => {
            const presentation = eventPresentation(event);
            const expanded = selected === event.event_id;
            const related = Boolean(event.call_id && selectedEvent?.call_id === event.call_id);
            const eventPanel = `${panelId}-${event.sequence}`;
            return (
              <div
                id={`${panelId}-event-${event.sequence}`}
                className={`event tone-${presentation.tone} ${event.attributes.injected === true ? 'injected' : ''} ${related ? 'related' : ''}`}
                key={event.event_id}
                data-related={related || undefined}
                data-event-id={event.event_id}
              >
                <button
                  className="event-button"
                  aria-expanded={expanded}
                  aria-controls={eventPanel}
                  onClick={() => setSelected(expanded ? null : event.event_id)}
                >
                  <span className="event-sequence">
                    {event.sequence.toString().padStart(2, '0')}
                  </span>
                  <span className="event-icon">
                    <EventIcon event={event} />
                  </span>
                  <span className="event-text">
                    <strong>{presentation.label}</strong>
                    <span>{event.name ?? event.call_id ?? event.kind}</span>
                  </span>
                  {event.attributes.injected === true ? (
                    <span className="injected-marker">Injected</span>
                  ) : null}
                  <span className="event-time">
                    <span>
                      {signed(Date.parse(event.occurred_at) - Date.parse(run.started_at), ' ms')}
                    </span>
                    <time dateTime={event.occurred_at}>{utcTime(event.occurred_at)}</time>
                  </span>
                  <ChevronDown size={13} className={expanded ? 'chevron open' : 'chevron'} />
                </button>
                {expanded ? (
                  <div className="event-inspector" id={eventPanel}>
                    <div className="inspector-heading">
                      <strong>Recorded metadata</strong>
                      <code>{event.event_id}</code>
                    </div>
                    {event.call_id ? (
                      <p>
                        Call <code>{event.call_id}</code> · related events in this run:{' '}
                        {(byCall.get(event.call_id) ?? [])
                          .map((item) => `#${item.sequence}`)
                          .join(', ')}
                      </p>
                    ) : (
                      <p>Lifecycle event · no call ID.</p>
                    )}
                    {event.attributes.timeout_simulated === true ||
                    event.attributes.fault_kind === 'tool_timeout' ? (
                      <p className="simulation-note">
                        Simulated tool-boundary timeout; this is not a recorded network failure.
                      </p>
                    ) : null}
                    <pre>{JSON.stringify(event, null, 2)}</pre>
                  </div>
                ) : null}
              </div>
            );
          })
        )}
      </div>
      <footer className="trace-footer">
        <span>Run ID</span>
        <code>{run.run_id}</code>
        <span>Adapter</span>
        <code>{run.adapter}</code>
      </footer>
    </section>
  );
}

export function RunOutput({ run, title }: { run: AgentRun; title: string }) {
  return (
    <section className="output-panel">
      <h3>{title} output</h3>
      <span className="muted small">Recorded final output</span>
      {run.output === null ? (
        <p className="muted">No final output recorded.</p>
      ) : (
        <pre>
          {typeof run.output === 'string' ? run.output : JSON.stringify(run.output, null, 2)}
        </pre>
      )}
    </section>
  );
}
