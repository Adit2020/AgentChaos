import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { ArrowRight, Play, ShieldCheck, TriangleAlert } from 'lucide-react';
import { ApiError, fetchExport, fetchScenarios, submitJob } from '../api/client';
import { jobRequestSchema, type JobStatus, type Scenario } from '../api/contracts';
import { Badge, PageState } from '../components/ui';
import { useResource } from '../hooks/useResource';
import { isActiveJob, useJob } from '../hooks/useJob';
import { faultLabel, localDate, type Tone } from '../lib/presentation';

const jobPresentation: Record<JobStatus['state'], { label: string; tone: Tone; note: string }> = {
  queued: {
    label: 'Queued',
    tone: 'neutral',
    note: 'The server accepted this experiment and is preparing execution.',
  },
  running: {
    label: 'Running',
    tone: 'accent',
    note: 'The server is executing a baseline/faulted pair. Recorded traces become available after export.',
  },
  completed: {
    label: 'Completed',
    tone: 'success',
    note: 'The experiment pair and its artifacts were saved. Inspect the results for task success and recovery evidence.',
  },
  failed: {
    label: 'Failed',
    tone: 'danger',
    note: 'The job did not complete normally. Available evidence is shown below.',
  },
  timed_out: {
    label: 'Timed out',
    tone: 'warning',
    note: 'The execution deadline was exceeded. This does not confirm provider processing or billing has stopped.',
  },
};
const failureMessages: Record<string, string> = {
  execution_failed:
    'Experiment execution failed. Check the server configuration before starting another job.',
  artifact_export_failed: 'The execution finished, but artifacts could not be saved.',
  execution_timeout: 'The execution deadline was exceeded and cancellation was requested.',
  server_shutdown: 'The server stopped while this job was active.',
};

function SavedEvidence({ job }: { job: JobStatus & { export_id: string } }) {
  const loader = useCallback(
    (signal: AbortSignal) => fetchExport(job.export_id, signal),
    [job.export_id],
  );
  const { state, refresh } = useResource(loader);
  const complete = job.state === 'completed' && !job.partial_export;
  if (state.status === 'loading')
    return (
      <p role="status" className="muted">
        Checking saved {complete ? 'results' : 'evidence'}…
      </p>
    );
  if (state.status === 'error' || state.data.artifact_type !== 'experiment')
    return (
      <div className="playground-notice" role="alert">
        <p>Saved evidence cannot currently be opened.</p>
        {state.status === 'error' ? <p>{state.error.message}</p> : null}
        <button className="button" onClick={refresh}>
          Check saved evidence
        </button>
      </div>
    );
  return (
    <div className="saved-evidence">
      {!complete ? (
        <p>
          {job.partial_export
            ? 'This export contains partial execution evidence.'
            : 'A recorded pair is available from this job.'}{' '}
          The job remains {jobPresentation[job.state].label.toLowerCase()}; this is not a completed
          job result.
        </p>
      ) : null}
      <a
        className={`button ${complete ? 'primary-button' : ''}`}
        href={`#/exports/${encodeURIComponent(job.export_id)}`}
      >
        {complete
          ? 'View Results'
          : job.partial_export
            ? 'View partial evidence'
            : 'View recorded evidence'}
        <ArrowRight size={16} />
      </a>
    </div>
  );
}

function JobMonitor({ jobId }: { jobId: string }) {
  const { job, error, checking, refresh } = useJob(jobId);
  const missing = error?.kind === 'missing';
  return (
    <section className="playground-card job-panel" aria-label="Experiment job">
      <div className="section-heading">
        <h2>Experiment job</h2>
        <button className="button" disabled={checking} onClick={refresh}>
          {checking ? 'Checking…' : 'Check status'}
        </button>
      </div>
      <code className="job-id">{jobId}</code>
      {error ? (
        <div className="playground-notice" role="alert">
          <strong>{missing ? 'Job no longer available' : 'Status check interrupted'}</strong>
          <p>{error.message}</p>
          {job && !missing ? <p>Last reported status is shown below.</p> : null}
          {error.kind === 'network' || error.kind === 'server' ? (
            <p>
              {job && !isActiveJob(job)
                ? 'Use Check status to try again.'
                : 'Status checks will retry; no experiment will be resubmitted.'}
            </p>
          ) : null}
        </div>
      ) : null}
      {!job && !error ? (
        <p className="muted" role="status">
          Loading job status…
        </p>
      ) : null}
      {job && !missing ? (
        <>
          <div className="job-state" aria-live="polite">
            <Badge tone={jobPresentation[job.state].tone}>{jobPresentation[job.state].label}</Badge>
            <p>{jobPresentation[job.state].note}</p>
          </div>
          <dl className="job-timestamps">
            <div>
              <dt>Scenario</dt>
              <dd>{job.scenario_id}</dd>
            </div>
            <div>
              <dt>Submitted · local time</dt>
              <dd>
                <time dateTime={job.submitted_at}>{localDate(job.submitted_at)}</time>
              </dd>
            </div>
            <div>
              <dt>Started · local time</dt>
              <dd>
                {job.started_at ? (
                  <time dateTime={job.started_at}>{localDate(job.started_at)}</time>
                ) : (
                  'Not reported'
                )}
              </dd>
            </div>
            <div>
              <dt>Finished · local time</dt>
              <dd>
                {job.finished_at ? (
                  <time dateTime={job.finished_at}>{localDate(job.finished_at)}</time>
                ) : (
                  'Not reported'
                )}
              </dd>
            </div>
          </dl>
          {job.error ? (
            <p className="playground-notice">
              {failureMessages[job.error.code] ??
                'The server reported a job error. Inspect any available evidence.'}
            </p>
          ) : null}
          {job.cancellation_pending ? (
            <div role="status" className="playground-notice cost-notice">
              <strong>Cancellation is still pending</strong>
              <p>
                The execution coroutine has not stopped. The server keeps its execution slot
                reserved. Use Check status for an update.
              </p>
            </div>
          ) : null}
          {job.export_id && !isActiveJob(job) ? (
            <SavedEvidence job={{ ...job, export_id: job.export_id }} />
          ) : null}
          {job.state === 'completed' && !job.export_id ? (
            <p className="playground-notice">No saved export was reported for this job.</p>
          ) : null}
          <p className="playground-caption">
            {isActiveJob(job)
              ? 'Status is checked about every two seconds.'
              : 'Automatic polling has stopped.'}{' '}
            This page does not stream live traces.
          </p>
        </>
      ) : null}
      <div className="job-links">
        <a href="#/">Experiment history</a>
        {missing || (job && !isActiveJob(job) && !job.cancellation_pending) ? (
          <a href="#/playground">Configure another experiment</a>
        ) : null}
      </div>
    </section>
  );
}

function ExperimentForm({ scenarios, onReload }: { scenarios: Scenario[]; onReload: () => void }) {
  const [scenarioId, setScenarioId] = useState(scenarios[0].scenario_id);
  const scenario = scenarios.find((item) => item.scenario_id === scenarioId);
  const [fault, setFault] = useState<string>(scenarios[0].supported_fault_types[0]);
  const [tool, setTool] = useState(scenarios[0].supported_target_tools[0]);
  const [occurrence, setOccurrence] = useState(1);
  const [acknowledged, setAcknowledged] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [uncertain, setUncertain] = useState(false);
  const [configurationRejected, setConfigurationRejected] = useState(false);
  const sending = useRef(false);
  const submission = useRef<AbortController | null>(null);
  useEffect(() => () => submission.current?.abort(), []);

  function selectScenario(id: string) {
    const selected = scenarios.find((item) => item.scenario_id === id);
    setScenarioId(id);
    setFault(selected?.supported_fault_types[0] ?? '');
    setTool(selected?.supported_target_tools[0] ?? '');
    setOccurrence(1);
    setAcknowledged(false);
    setError(null);
  }
  async function run(event: FormEvent) {
    event.preventDefault();
    if (sending.current) return;
    setError(null);
    setConfigurationRejected(false);
    if (!acknowledged) {
      setError('Acknowledge the possible model API charges before running.');
      return;
    }
    const parsed = jobRequestSchema.safeParse({
      scenario_id: scenarioId,
      fault_type: fault,
      target_tool: tool,
      occurrence,
      acknowledge_live_cost: acknowledged,
    });
    if (
      !parsed.success ||
      !scenario ||
      !scenario.supported_fault_types.some((item) => item === fault) ||
      !scenario.supported_target_tools.includes(tool) ||
      occurrence > scenario.max_occurrence
    ) {
      setError('Choose a supported scenario, fault, tool, and occurrence.');
      return;
    }
    sending.current = true;
    setPending(true);
    const controller = new AbortController();
    submission.current = controller;
    try {
      const job = await submitJob(parsed.data, controller.signal);
      if (!controller.signal.aborted)
        window.location.hash = `#/playground/jobs/${encodeURIComponent(job.job_id)}`;
    } catch (caught) {
      if (controller.signal.aborted) return;
      const failure =
        caught instanceof ApiError
          ? caught
          : new ApiError('server', 'The experiment could not be submitted.');
      setError(failure.message);
      setConfigurationRejected(failure.kind === 'invalid' && !failure.submissionUncertain);
      setUncertain(failure.submissionUncertain);
      setAcknowledged(false);
      sending.current = false;
      setPending(false);
    }
  }
  return (
    <div className="playground-layout">
      <section className="playground-card configuration-card" aria-label="Experiment configuration">
        <div className="section-heading">
          <h2>Configure an experiment</h2>
          <Badge tone="accent">Single-run pair</Badge>
        </div>
        <form onSubmit={run} noValidate>
          <fieldset disabled={pending}>
            <legend className="sr-only">Scenario and controlled fault</legend>
            <label htmlFor="scenario">Registered scenario</label>
            <select
              id="scenario"
              value={scenarioId}
              onChange={(event) => selectScenario(event.target.value)}
            >
              {scenarios.map((item) => (
                <option key={item.scenario_id} value={item.scenario_id}>
                  {item.name}
                </option>
              ))}
            </select>
            {scenario ? (
              <div className="scenario-context">
                <p>{scenario.description}</p>
                <span className="field-caption">Server-defined task</span>
                <p className="fixed-task">{scenario.default_task}</p>
              </div>
            ) : null}
            <div className="playground-fields">
              <div>
                <label htmlFor="fault">Fault type</label>
                <select
                  id="fault"
                  value={fault}
                  onChange={(event) => {
                    setFault(event.target.value);
                    setAcknowledged(false);
                  }}
                >
                  {scenario?.supported_fault_types.map((item) => (
                    <option value={item} key={item}>
                      {faultLabel(item)}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="target-tool">Target tool</label>
                <select
                  id="target-tool"
                  value={tool}
                  onChange={(event) => {
                    setTool(event.target.value);
                    setAcknowledged(false);
                  }}
                >
                  {scenario?.supported_target_tools.map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
              </div>
            </div>
            <label htmlFor="occurrence">Target occurrence</label>
            <input
              id="occurrence"
              type="number"
              min={1}
              max={scenario?.max_occurrence}
              step={1}
              value={Number.isNaN(occurrence) ? '' : occurrence}
              onChange={(event) => {
                setOccurrence(event.target.valueAsNumber);
                setAcknowledged(false);
              }}
              aria-describedby="occurrence-help"
            />
            <p id="occurrence-help" className="field-caption">
              Select tool call 1–{scenario?.max_occurrence}. The configured fault may not trigger if
              that occurrence is never reached.
            </p>
            {fault === 'tool_timeout' ? (
              <p className="playground-notice">
                This fault simulates a tool-boundary timeout; it is not a real network timeout.
              </p>
            ) : null}
            <div className="cost-notice">
              <TriangleAlert size={18} />
              <div>
                <strong>Live execution may incur charges</strong>
                <p>
                  One baseline and one faulted agent execution will run. Each may make multiple paid
                  OpenAI model calls.
                </p>
                <label className="cost-checkbox">
                  <input
                    type="checkbox"
                    required
                    checked={acknowledged}
                    onChange={(event) => setAcknowledged(event.target.checked)}
                  />
                  I acknowledge that this experiment may incur real model API charges.
                </label>
              </div>
            </div>
            {error ? (
              <div className="playground-notice" role="alert">
                <p>{error}</p>
                {configurationRejected ? (
                  <button type="button" className="button" onClick={onReload}>
                    Reload scenarios
                  </button>
                ) : null}
                {uncertain ? (
                  <p>
                    Submission was not confirmed. The server may have accepted the job. Check{' '}
                    <a href="#/">experiment history</a> before submitting again. No automatic
                    submission retry was made.
                  </p>
                ) : null}
              </div>
            ) : null}
            <button
              type="submit"
              className="button primary-button run-button"
              disabled={pending || !scenario}
            >
              <Play size={15} />
              {pending ? 'Submitting…' : 'Run Experiment'}
            </button>
            <p className="playground-caption">
              Leaving or refreshing this page does not cancel a submitted experiment.
            </p>
          </fieldset>
        </form>
      </section>
      <aside className="playground-card playground-guide">
        <ShieldCheck size={22} />
        <h2>Controlled fault. Recorded evidence.</h2>
        <ol>
          <li>
            <strong>Baseline</strong>
            <span>Run the fixed task without an injected fault.</span>
          </li>
          <li>
            <strong>Faulted execution</strong>
            <span>Run the same task with your selected tool fault.</span>
          </li>
          <li>
            <strong>Inspect the difference</strong>
            <span>Open saved traces, comparison metrics, and recovery evidence.</span>
          </li>
        </ol>
        <p className="playground-caption">
          One experiment can be active at a time. Continuation, recovery, and task success are
          evaluated separately.
        </p>
      </aside>
    </div>
  );
}

export function PlaygroundPage({ jobId }: { jobId?: string }) {
  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Experiment Playground</h1>
          <p>Run a trusted scenario. Inspect how an agent handles a controlled tool failure.</p>
        </div>
      </div>
      {jobId ? <JobMonitor key={jobId} jobId={jobId} /> : <ScenarioConfiguration />}
    </>
  );
}
function ScenarioConfiguration() {
  const { state, refresh } = useResource(fetchScenarios);
  if (state.status === 'loading')
    return <PageState type="loading" title="Loading registered scenarios" />;
  if (state.status === 'error')
    return (
      <PageState type="error" title="Could not load scenarios" retry={refresh}>
        {state.error.message}
      </PageState>
    );
  if (state.data.scenarios.length === 0)
    return (
      <PageState type="empty" title="No scenarios registered" retry={refresh}>
        Ask the server operator to register a trusted scenario. No experiment can be started yet.
      </PageState>
    );
  return (
    <ExperimentForm
      key={JSON.stringify(state.data.scenarios)}
      scenarios={state.data.scenarios}
      onReload={refresh}
    />
  );
}
