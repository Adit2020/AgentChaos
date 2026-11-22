import { useCallback, useState } from 'react';
import { ArrowDownToLine, ArrowLeft, TriangleAlert } from 'lucide-react';
import { fetchExport } from '../api/client';
import type { SingleArtifact } from '../api/contracts';
import { useResource } from '../hooks/useResource';
import { faultLabel, localDate, milliseconds, signed, verdict } from '../lib/presentation';
import { MetricCard, PageState, RefreshButton, VerdictBadge } from '../components/ui';
import { EventTimeline, RunOutput } from '../components/EventTimeline';
import { MultiRunInspection } from './MultiRunInspection';

export function SingleInspection({ artifact }: { artifact: SingleArtifact }) {
  const { manifest, comparison, baseline, faulted } = artifact;
  const recovery = verdict(comparison.recovered, true);
  const fault = manifest.experiment.fault;
  const injectedEvents = faulted.events.filter((event) => event.attributes.injected === true);
  const identifiedInjectedEvent =
    comparison.fault_triggered && injectedEvents.length === 1 ? injectedEvents[0] : null;
  const [injectedEventRequest, setInjectedEventRequest] = useState<{
    eventId: string;
    token: number;
  } | null>(null);
  return (
    <>
      <div className="page-heading inspection-heading">
        <div>
          <h1>Experiment inspection</h1>
          <p className="task-text">{manifest.experiment.task ?? baseline.task}</p>
        </div>
        <span className="muted small">{localDate(manifest.exported_at)}</span>
      </div>
      <div className="export-id mono">{manifest.export_id}</div>
      <section className="fault-band" aria-label="Configured fault">
        <TriangleAlert size={16} />
        <span>Configured fault</span>
        <strong>{faultLabel(fault.fault_type)}</strong>
        <span className="divider" />
        <span>Target</span>
        <code>{fault.target_tool ?? fault.target_event_id ?? 'Not recorded'}</code>
        <span className="divider" />
        <span>Occurrence</span>
        <code>{fault.occurrence}</code>
      </section>
      {fault.fault_type === 'tool_timeout' ? (
        <p className="small muted">
          This timeout is a controlled tool-boundary simulation, not a real network timeout.
        </p>
      ) : null}
      <section className="outcome-panel" aria-label="Execution outcome">
        <div className="section-heading outcome-heading">
          <div>
            <h2>What happened</h2>
            <p>Observed statuses and task evaluation from this artifact.</p>
          </div>
          {identifiedInjectedEvent ? (
            <button
              className="button injected-event-link"
              onClick={() =>
                setInjectedEventRequest((current) => ({
                  eventId: identifiedInjectedEvent.event_id,
                  token: (current?.token ?? 0) + 1,
                }))
              }
            >
              <ArrowDownToLine size={14} />
              View injected event
            </button>
          ) : null}
        </div>
        <div className="metric-grid outcome-state-grid">
          <MetricCard
            label="Configured fault triggered"
            value={comparison.fault_triggered ? 'Yes' : 'No'}
            note="Correlated injection evidence"
            tone={comparison.fault_triggered ? 'warning' : 'neutral'}
          />
          <MetricCard
            label="Agent continued"
            value={comparison.continued_after_fault ? 'Yes' : 'No'}
            note="Post-fault model event observed"
            tone="neutral"
          />
          <MetricCard
            label="Recovery"
            value={recovery.label}
            note={
              comparison.recovered === null
                ? 'Insufficient evidence for a verdict'
                : 'Task evaluation and trace evidence'
            }
            tone={recovery.tone}
          />
          <MetricCard
            label="Task success"
            value={
              <span className="task-verdict-pair">
                <span>
                  <small>Baseline</small>
                  <VerdictBadge value={comparison.baseline_task_success} />
                </span>
                <span>
                  <small>Faulted</small>
                  <VerdictBadge value={comparison.faulted_task_success} />
                </span>
              </span>
            }
            note="Evaluated independently for each run"
          />
        </div>
        <div className="execution-cost">
          <div className="execution-cost-heading">
            <h3>Observed execution cost</h3>
            <span>Recorded duration and attempted calls · no monetary cost is recorded</span>
          </div>
          <div className="execution-cost-grid">
            <div>
              <span>Duration</span>
              <strong>Baseline · {milliseconds(comparison.baseline_duration_ms)}</strong>
              <strong>Faulted · {milliseconds(comparison.faulted_duration_ms)}</strong>
              <small>Change · {signed(comparison.duration_delta_ms, ' ms')}</small>
            </div>
            <div>
              <span>Model calls</span>
              <strong>
                {comparison.baseline_model_calls} baseline · {comparison.faulted_model_calls}{' '}
                faulted
              </strong>
              <small>Change · {signed(comparison.model_call_delta)}</small>
            </div>
            <div>
              <span>Tool calls</span>
              <strong>
                {comparison.baseline_tool_calls} baseline · {comparison.faulted_tool_calls} faulted
              </strong>
              <small>Change · {signed(comparison.tool_call_delta)}</small>
            </div>
          </div>
        </div>
      </section>
      <div className="section-heading trace-section-heading">
        <h2>Execution traces</h2>
        <span className="muted small">
          Ordered independently · call IDs correlate within each run
        </span>
      </div>
      <div className="trace-grid">
        <EventTimeline run={baseline} title="Baseline" duration={comparison.baseline_duration_ms} />
        <EventTimeline
          run={faulted}
          title="Faulted"
          duration={comparison.faulted_duration_ms}
          focusRequest={injectedEventRequest}
        />
      </div>
      <div className="trace-grid output-grid">
        <RunOutput run={baseline} title="Baseline" />
        <RunOutput run={faulted} title="Faulted" />
      </div>
      <section className="comparison-section" aria-label="Comparison metrics">
        <div className="section-heading">
          <h2>Comparison</h2>
          <span className="muted small">Attempted calls, including unfinished calls</span>
        </div>
        <div className="comparison-layout">
          <table className="comparison-table">
            <thead>
              <tr>
                <th>Metric</th>
                <th>Baseline</th>
                <th>Faulted</th>
                <th>Delta</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <th>Model calls</th>
                <td>{comparison.baseline_model_calls}</td>
                <td>{comparison.faulted_model_calls}</td>
                <td>{signed(comparison.model_call_delta)}</td>
              </tr>
              <tr>
                <th>Tool calls</th>
                <td>{comparison.baseline_tool_calls}</td>
                <td>{comparison.faulted_tool_calls}</td>
                <td>{signed(comparison.tool_call_delta)}</td>
              </tr>
              <tr>
                <th>Duration</th>
                <td>{milliseconds(comparison.baseline_duration_ms)}</td>
                <td>{milliseconds(comparison.faulted_duration_ms)}</td>
                <td>{signed(comparison.duration_delta_ms, ' ms')}</td>
              </tr>
              <tr>
                <th>Task success</th>
                <td>
                  <VerdictBadge value={comparison.baseline_task_success} />
                </td>
                <td>
                  <VerdictBadge value={comparison.faulted_task_success} />
                </td>
                <td className="muted">—</td>
              </tr>
              <tr>
                <th>Injected failures</th>
                <td>{comparison.baseline_injected_failures}</td>
                <td>{comparison.faulted_injected_failures}</td>
                <td className="muted">—</td>
              </tr>
            </tbody>
          </table>
          <div className="recovery-evidence">
            <h3>Recovery evidence</h3>
            <dl>
              <div>
                <dt>Post-fault target-tool calls</dt>
                <dd>{comparison.target_tool_retries}</dd>
              </div>
              <div>
                <dt>Completed alternative-tool calls</dt>
                <dd>{comparison.alternative_tool_calls}</dd>
              </div>
            </dl>
            <p>
              Continuation and retry attempts are evidence. Confirmed recovery also requires
              successful task evaluation and a completed target-tool retry.
            </p>
            {comparison.recovered === null ? (
              <p className="unknown-note">
                Unknown recovery is an unevaluated or inconclusive outcome.
              </p>
            ) : null}
          </div>
        </div>
      </section>
    </>
  );
}

export function DetailPage({ exportId }: { exportId: string }) {
  const loader = useCallback((signal: AbortSignal) => fetchExport(exportId, signal), [exportId]);
  const { state, refresh } = useResource(loader);
  return (
    <>
      <div className="detail-toolbar">
        <a className="back-link" href="#/">
          <ArrowLeft size={14} />
          Back to history
        </a>
        <RefreshButton onClick={refresh} />
      </div>
      {state.status === 'loading' ? (
        <PageState type="loading" title="Loading recorded experiment" />
      ) : state.status === 'error' ? (
        <PageState
          type="error"
          title={state.error.kind === 'missing' ? 'Export not found' : 'Could not load experiment'}
          retry={refresh}
        >
          {state.error.message}
        </PageState>
      ) : state.data.artifact_type === 'experiment' ? (
        <SingleInspection artifact={state.data.artifact} />
      ) : (
        <MultiRunInspection artifact={state.data.artifact} />
      )}
    </>
  );
}
