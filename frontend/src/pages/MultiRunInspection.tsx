import { useState } from 'react';
import { Layers, TriangleAlert } from 'lucide-react';
import type { MultiArtifact } from '../api/contracts';
import { EventTimeline } from '../components/EventTimeline';
import { Badge, MetricCard, VerdictBadge } from '../components/ui';
import { faultLabel, localDate, milliseconds, signed } from '../lib/presentation';

type Summary = MultiArtifact['summary'];
type Repetition = MultiArtifact['repetitions'][number];
type RecordedOutcome = NonNullable<Repetition['outcome']>;

function numeric(summary: Summary, key: string): number | null {
  const value = summary[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function count(summary: Summary, key: string): string {
  return numeric(summary, key)?.toLocaleString() ?? 'Unavailable';
}

function denominator(summary: Summary, key: string): string {
  const value = numeric(summary, key);
  return value === null ? 'denominator unavailable' : `n = ${value}`;
}

function MetricBar({
  title,
  rows,
  note,
}: {
  title: string;
  rows: { label: string; value: number | null; detail: string; tone: string }[];
  note?: string;
}) {
  const usable = rows.some((row) => row.value !== null);
  return (
    <section className="analytics-card">
      <h3>{title}</h3>
      {note ? <p className="analytics-note">{note}</p> : null}
      {!usable ? (
        <p className="analytics-unavailable">Metric unavailable in recorded data.</p>
      ) : (
        <div className="rate-chart">
          {rows.map((row) => (
            <div className="rate-row" key={row.label}>
              <div className="rate-label">
                <span>{row.label}</span>
                <span className="mono">{row.detail}</span>
              </div>
              <div
                className="rate-track"
                role="img"
                aria-label={`${row.label}: ${row.value === null ? 'unavailable' : `${(row.value * 100).toFixed(1)} percent`}`}
              >
                {row.value === null ? (
                  <span className="rate-unknown">Unknown</span>
                ) : (
                  <span
                    className={`rate-fill ${row.tone}`}
                    style={{ width: `${Math.max(0, Math.min(row.value, 1)) * 100}%` }}
                  />
                )}
              </div>
              <strong>
                {row.value === null ? 'Unavailable' : `${(row.value * 100).toFixed(1)}%`}
              </strong>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function RecoveryChart({ summary }: { summary: Summary }) {
  const keys = [
    ['Confirmed', 'confirmed_recoveries', 'success'],
    ['Not recovered', 'confirmed_nonrecoveries', 'danger'],
    ['Unknown', 'unknown_recoveries', 'neutral'],
  ] as const;
  const values = keys.map(([label, key, tone]) => ({ label, value: numeric(summary, key), tone }));
  const available = values.every((entry) => entry.value !== null);
  const total = available ? values.reduce((sum, entry) => sum + entry.value!, 0) : 0;
  const confirmedRecoveryRate = numeric(summary, 'confirmed_recovery_rate');
  const requestedRecoveryRate = numeric(summary, 'recovery_rate_requested');
  const evaluableTriggered = numeric(summary, 'evaluable_triggered_repetitions');
  const requested = numeric(summary, 'requested_repetitions');
  return (
    <section className="analytics-card">
      <h3>Recovery outcomes</h3>
      <p className="analytics-note">
        Recorded repetitions only. Unknown includes outcomes without an evaluable recovery verdict.
      </p>
      <div className="recovery-rates">
        <div className="recovery-rate">
          <span>Confirmed recovery rate</span>
          <strong>
            {confirmedRecoveryRate === null
              ? 'Unavailable'
              : `${(confirmedRecoveryRate * 100).toFixed(1)}%`}
          </strong>
          <small>
            {evaluableTriggered === null
              ? 'evaluable triggered repetitions unavailable'
              : `${evaluableTriggered} evaluable triggered repetitions`}
          </small>
        </div>
        <div className="recovery-rate">
          <span>Confirmed recoveries / planned pairs</span>
          <strong>
            {requestedRecoveryRate === null
              ? 'Unavailable'
              : `${(requestedRecoveryRate * 100).toFixed(1)}%`}
          </strong>
          <small>
            {requested === null ? 'planned pairs unavailable' : `${requested} planned pairs`}
          </small>
        </div>
      </div>
      {!available ? (
        <p className="analytics-unavailable">Outcome counts unavailable in this report.</p>
      ) : total === 0 ? (
        <p className="analytics-unavailable">No recorded repetitions.</p>
      ) : (
        <>
          <div className="stacked-track" role="img" aria-label="Recovery outcome counts">
            {values.map((entry) => (
              <span
                className={`stacked-segment ${entry.tone}`}
                key={entry.label}
                style={{ width: `${(entry.value! / total) * 100}%` }}
                title={`${entry.label}: ${entry.value}`}
              />
            ))}
          </div>
          <ul className="chart-legend">
            {values.map((entry) => (
              <li key={entry.label}>
                <span className={`legend-dot ${entry.tone}`} />
                {entry.label}
                <strong>{entry.value}</strong>
              </li>
            ))}
          </ul>
          <p className="analytics-note">
            Confirmed recovery uses evaluable triggered repetitions. The requested-pair rate uses
            the planned count; unknown outcomes are not counted as failures.
          </p>
        </>
      )}
    </section>
  );
}

function LatencyChart({ repetitions }: { repetitions: Repetition[] }) {
  const points = repetitions.flatMap((repetition) => {
    if (!repetition.outcome) return [];
    const comparison = repetition.outcome.comparison;
    return [
      {
        number: repetition.number,
        baseline: comparison.baseline_duration_ms,
        faulted: comparison.faulted_duration_ms,
      },
    ];
  });
  const maximum = Math.max(
    0,
    ...points.flatMap((point) => [point.baseline ?? 0, point.faulted ?? 0]),
  );
  return (
    <section className="analytics-card latency-card">
      <h3>Latency by repetition</h3>
      <p className="analytics-note">
        Recorded durations only; missing durations remain unavailable.
      </p>
      {points.length === 0 ? (
        <p className="analytics-unavailable">No recorded repetitions to compare.</p>
      ) : (
        <>
          <div className="latency-legend">
            <span>
              <i className="baseline-key" />
              Baseline
            </span>
            <span>
              <i className="faulted-key" />
              Faulted
            </span>
          </div>
          <div
            className="latency-chart"
            role="img"
            aria-label="Baseline and faulted duration for each recorded repetition"
          >
            {points.map((point) => (
              <div className="latency-row" key={point.number}>
                <span className="latency-repetition">#{point.number}</span>
                <div className="latency-bars">
                  {[
                    ['Baseline', point.baseline, 'baseline'],
                    ['Faulted', point.faulted, 'faulted'],
                  ].map(([label, raw, style]) => {
                    const value = typeof raw === 'number' ? raw : null;
                    return (
                      <div className="latency-bar-row" key={label}>
                        <span className="sr-only">{label}</span>
                        {value === null ? (
                          <span className="latency-missing">Unavailable</span>
                        ) : (
                          <span
                            className={`latency-bar ${style}`}
                            style={{ width: `${maximum ? (value / maximum) * 100 : 0}%` }}
                          />
                        )}
                        <code>{milliseconds(value)}</code>
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </section>
  );
}

function RecoveryResult({ outcome }: { outcome: RecordedOutcome }) {
  if (!outcome.comparison.fault_triggered) return <Badge tone="neutral">No fault triggered</Badge>;
  return <VerdictBadge value={outcome.comparison.recovered} recovery />;
}

function FaultTriggerResult({ triggered }: { triggered: boolean }) {
  return <Badge tone={triggered ? 'warning' : 'neutral'}>{triggered ? 'Yes' : 'No'}</Badge>;
}

function RepetitionRow({ repetition }: { repetition: Repetition }) {
  const [expanded, setExpanded] = useState(false);
  const outcome = repetition.outcome;
  const stateTone =
    repetition.state === 'recorded'
      ? 'success'
      : repetition.state === 'incomplete'
        ? 'warning'
        : 'neutral';
  return (
    <article className="repetition-card">
      <div className="repetition-topline">
        <div className="repetition-name">
          <h3>Repetition {repetition.number.toString().padStart(2, '0')}</h3>
          <Badge tone={stateTone}>{repetition.state.replace('_', ' ')}</Badge>
        </div>
        {outcome ? (
          <button
            className="button repetition-toggle"
            aria-expanded={expanded}
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? 'Hide traces' : 'Inspect traces'}
          </button>
        ) : null}
      </div>
      {!outcome ? (
        <p className="repetition-unavailable">
          {repetition.state === 'incomplete'
            ? 'This repetition started but did not produce a complete baseline/faulted pair. Execution details were not recorded.'
            : 'This repetition did not start. No execution details are available.'}
        </p>
      ) : (
        <>
          <div className="repetition-metrics">
            <div>
              <span>Fault triggered</span>
              <FaultTriggerResult triggered={outcome.comparison.fault_triggered} />
            </div>
            <div>
              <span>Recovery</span>
              <RecoveryResult outcome={outcome} />
            </div>
            <div>
              <span>Task success · baseline</span>
              <VerdictBadge value={outcome.comparison.baseline_task_success} />
            </div>
            <div>
              <span>Task success · faulted</span>
              <VerdictBadge value={outcome.comparison.faulted_task_success} />
            </div>
            <div>
              <span>Duration · baseline</span>
              <strong>{milliseconds(outcome.comparison.baseline_duration_ms)}</strong>
            </div>
            <div>
              <span>Duration · faulted</span>
              <strong>{milliseconds(outcome.comparison.faulted_duration_ms)}</strong>
            </div>
            <div>
              <span>Model calls · baseline / faulted</span>
              <strong>
                {outcome.comparison.baseline_model_calls} / {outcome.comparison.faulted_model_calls}
              </strong>
            </div>
            <div>
              <span>Tool calls · baseline / faulted</span>
              <strong>
                {outcome.comparison.baseline_tool_calls} / {outcome.comparison.faulted_tool_calls}
              </strong>
            </div>
          </div>
          {expanded ? (
            <div className="trace-grid repetition-traces">
              <EventTimeline
                run={outcome.baseline}
                title={`Repetition ${repetition.number} · Baseline`}
                duration={outcome.comparison.baseline_duration_ms}
              />
              <EventTimeline
                run={outcome.faulted}
                title={`Repetition ${repetition.number} · Faulted`}
                duration={outcome.comparison.faulted_duration_ms}
              />
            </div>
          ) : null}
        </>
      )}
    </article>
  );
}

export function MultiRunInspection({ artifact }: { artifact: MultiArtifact }) {
  const { manifest, summary, repetitions } = artifact;
  const recorded = repetitions.filter((item) => item.state === 'recorded').length;
  const incomplete = repetitions.filter((item) => item.state === 'incomplete').length;
  const notStarted = repetitions.filter((item) => item.state === 'not_started').length;
  const planned = repetitions.length;
  const triggered = numeric(summary, 'triggered_faults');
  const completed = numeric(summary, 'completed_repetitions');
  const faultRate = numeric(summary, 'fault_trigger_rate');
  return (
    <>
      <div className="page-heading inspection-heading">
        <div>
          <h1>Multi-run reliability</h1>
          <p className="task-text">{manifest.experiment.task ?? 'Task not recorded'}</p>
        </div>
        <Badge tone="accent">Multi-run</Badge>
      </div>
      <div className="export-id mono">{manifest.export_id}</div>
      <section className="fault-band" aria-label="Configured fault">
        <TriangleAlert size={16} />
        <span>Configured fault</span>
        <strong>{faultLabel(manifest.experiment.fault.fault_type)}</strong>
        <span className="divider" />
        <span>Target</span>
        <code>
          {manifest.experiment.fault.target_tool ??
            manifest.experiment.fault.target_event_id ??
            'Not recorded'}
        </code>
        <span className="divider" />
        <span>Occurrence</span>
        <code>{manifest.experiment.fault.occurrence}</code>
        <span className="divider" />
        <span>Report</span>
        <code>{manifest.report_state}</code>
      </section>
      {manifest.experiment.fault.fault_type === 'tool_timeout' ? (
        <p className="small muted timeout-caption">
          Configured timeouts are simulated tool-boundary delays, not real network failures.
        </p>
      ) : null}
      <div className="metric-grid">
        <MetricCard label="Planned repetitions" value={planned} note="From the manifest" />
        <MetricCard
          label="Recorded pairs"
          value={recorded}
          note="Complete baseline/faulted outcomes"
          tone="success"
        />
        <MetricCard
          label="Incomplete"
          value={incomplete}
          note="Started without a complete pair"
          tone={incomplete ? 'warning' : 'neutral'}
        />
        <MetricCard label="Not started" value={notStarted} note="No execution details available" />
      </div>
      <div className="analytics-grid">
        <MetricBar
          title="Fault-trigger frequency"
          rows={[
            {
              label: 'Triggered',
              value: faultRate,
              detail: `${triggered ?? '—'} / ${completed ?? '—'} recorded pairs`,
              tone: 'warning',
            },
          ]}
          note="Only correlated injected-fault evidence counts as triggered."
        />
        <MetricBar
          title="Task success by run type"
          rows={[
            {
              label: 'Baseline',
              value: numeric(summary, 'baseline_task_success_rate'),
              detail: `${count(summary, 'baseline_task_successes')} / ${count(summary, 'baseline_evaluated_repetitions')} evaluated`,
              tone: 'success',
            },
            {
              label: 'Faulted',
              value: numeric(summary, 'faulted_task_success_rate'),
              detail: `${count(summary, 'faulted_task_successes')} / ${count(summary, 'faulted_evaluated_repetitions')} evaluated`,
              tone: 'accent',
            },
          ]}
          note="Unevaluated task outcomes are excluded from each denominator."
        />
        <RecoveryChart summary={summary} />
        <section className="analytics-card aggregate-card">
          <h3>Latency difference</h3>
          <p className="analytics-note">Faulted duration − baseline duration</p>
          <div className="latency-aggregate">
            <div>
              <span>Mean</span>
              <strong>{signed(numeric(summary, 'average_latency_delta_ms'), ' ms')}</strong>
            </div>
            <div>
              <span>Median</span>
              <strong>{signed(numeric(summary, 'median_latency_delta_ms'), ' ms')}</strong>
            </div>
          </div>
          <p className="analytics-note">
            {denominator(summary, 'latency_observations')} with both durations recorded
          </p>
        </section>
        <section className="analytics-card aggregate-card">
          <h3>Call-count difference</h3>
          <p className="analytics-note">Faulted − baseline; observed recorded pairs</p>
          <div className="latency-aggregate">
            <div>
              <span>Model · mean / median</span>
              <strong>
                {signed(numeric(summary, 'average_extra_model_calls'))} /{' '}
                {signed(numeric(summary, 'median_extra_model_calls'))}
              </strong>
            </div>
            <div>
              <span>Tool · mean / median</span>
              <strong>
                {signed(numeric(summary, 'average_extra_tool_calls'))} /{' '}
                {signed(numeric(summary, 'median_extra_tool_calls'))}
              </strong>
            </div>
          </div>
          <p className="analytics-note">
            {denominator(summary, 'model_call_observations')} model and{' '}
            {denominator(summary, 'tool_call_observations')} tool observations
          </p>
          <p className="analytics-note">Failed pairs: {count(summary, 'failed_pairs')}</p>
        </section>
        <LatencyChart repetitions={repetitions} />
      </div>
      <section className="repetition-section">
        <div className="section-heading">
          <h2>Repetition explorer</h2>
          <span className="muted small">
            {recorded} recorded · {incomplete} incomplete · {notStarted} not started
          </span>
        </div>
        {repetitions.length === 0 ? (
          <div className="repetition-empty">
            <Layers size={20} />
            <p>No repetitions were recorded in this manifest.</p>
          </div>
        ) : (
          <div className="repetition-list">
            {repetitions.map((repetition) => (
              <RepetitionRow key={repetition.number} repetition={repetition} />
            ))}
          </div>
        )}
      </section>
      <p className="analytics-footer">
        Exported {localDate(manifest.exported_at)} · Rates and means preserve the denominators in
        the reliability report.
      </p>
    </>
  );
}
