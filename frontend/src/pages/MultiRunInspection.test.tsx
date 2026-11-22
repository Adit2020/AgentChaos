import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { detailSchema } from '../api/contracts';
import type { MultiArtifact } from '../api/contracts';
import multiFixture from '../test/fixtures/multi.json';
import { MultiRunInspection } from './MultiRunInspection';

function loadMultiFixture(): MultiArtifact {
  const parsed = detailSchema.parse(multiFixture);
  if (parsed.artifact_type !== 'multi_run') throw new Error('Expected a multi-run fixture');
  return parsed.artifact;
}
const artifact = loadMultiFixture();

describe('multi-run reliability analytics', () => {
  it('shows report counts, denominators, rates, and supplied aggregate metrics', () => {
    render(<MultiRunInspection artifact={artifact} />);
    expect(screen.getByRole('heading', { name: 'Multi-run reliability' })).toBeVisible();
    expect(screen.getByText('Planned repetitions')).toBeVisible();
    expect(screen.getAllByText('6', { selector: 'strong' }).length).toBeGreaterThan(0);
    expect(screen.getByText('3 / 4 recorded pairs')).toBeVisible();
    expect(screen.getByText('75.0%')).toBeVisible();
    expect(screen.getByText('50.0%')).toBeVisible();
    expect(screen.getByText('16.7%')).toBeVisible();
    expect(screen.getByText('2 evaluable triggered repetitions')).toBeVisible();
    expect(screen.getByText('6 planned pairs')).toBeVisible();
    expect(screen.getAllByText('2 / 3 evaluated')).toHaveLength(2);
    expect(screen.getByText('+250 ms')).toBeVisible();
    expect(screen.getByText('+350 ms')).toBeVisible();
    expect(screen.getByText('Latency by repetition')).toBeVisible();
    expect(screen.getByText(/4 recorded · 1 incomplete · 1 not started/)).toBeVisible();
  });

  it('keeps unknown recovery separate from no fault and confirmed non-recovery', () => {
    render(<MultiRunInspection artifact={artifact} />);
    expect(screen.getAllByText('Confirmed').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Not recovered').length).toBeGreaterThan(0);
    expect(screen.getByText('No fault triggered')).toBeVisible();
    expect(screen.getAllByText('Unknown').length).toBeGreaterThan(0);
    expect(screen.getByText(/unknown outcomes are not counted as failures/)).toBeVisible();
  });

  it('shows why incomplete and not-started repetitions have no execution details', () => {
    render(<MultiRunInspection artifact={artifact} />);
    expect(screen.getByText(/started but did not produce a complete/)).toBeVisible();
    expect(screen.getByText(/did not start\. No execution details/)).toBeVisible();
    expect(screen.getByText('incomplete', { selector: 'span.badge' })).toBeVisible();
    expect(screen.getByText('not started', { selector: 'span.badge' })).toBeVisible();
  });

  it('expands recorded repetitions into the existing baseline and faulted trace viewers', async () => {
    render(<MultiRunInspection artifact={artifact} />);
    await userEvent.click(screen.getAllByRole('button', { name: 'Inspect traces' })[0]);
    const baselineTrace = screen.getByRole('region', {
      name: 'Repetition 1 · Baseline execution trace',
    });
    expect(baselineTrace).toBeVisible();
    expect(
      screen.getByRole('region', { name: 'Repetition 1 · Faulted execution trace' }),
    ).toBeVisible();
    await userEvent.click(within(baselineTrace).getByRole('button', { name: /Tool call/ }));
    expect(screen.getByText(/related events in this run/)).toBeVisible();
  });

  it('represents a batch with no recorded pairs without drawing zero-valued outcomes', () => {
    const empty = structuredClone(artifact);
    if (empty.manifest.artifact_type !== 'multi_run') throw new Error('Expected multi-run');
    empty.manifest.repetitions = empty.manifest.repetitions.map((item) => ({
      number: item.number,
      state: 'not_started',
      directory: null,
      experiment_id: null,
      baseline_run_id: null,
      faulted_run_id: null,
      baseline_status: null,
      faulted_status: null,
      fault_triggered: null,
      recovered: null,
    }));
    empty.summary = {
      requested_repetitions: 6,
      completed_repetitions: 0,
      incomplete_pairs: 0,
      not_started_repetitions: 6,
      triggered_faults: 0,
      fault_trigger_rate: null,
      evaluable_triggered_repetitions: 0,
      confirmed_recoveries: 0,
      confirmed_nonrecoveries: 0,
      unknown_recoveries: 0,
      confirmed_recovery_rate: null,
      baseline_task_successes: 0,
      baseline_evaluated_repetitions: 0,
      baseline_task_success_rate: null,
      faulted_task_successes: 0,
      faulted_evaluated_repetitions: 0,
      faulted_task_success_rate: null,
      latency_observations: 0,
      average_latency_delta_ms: null,
      median_latency_delta_ms: null,
      model_call_observations: 0,
      average_extra_model_calls: null,
      median_extra_model_calls: null,
      tool_call_observations: 0,
      average_extra_tool_calls: null,
      median_extra_tool_calls: null,
    };
    empty.repetitions = empty.manifest.repetitions.map((item) => ({
      number: item.number,
      state: item.state,
      outcome: null,
    }));
    render(<MultiRunInspection artifact={empty} />);
    expect(screen.getByText('No recorded repetitions.')).toBeVisible();
    expect(screen.getByText('No recorded repetitions to compare.')).toBeVisible();
    expect(
      screen.getAllByText('This repetition did not start. No execution details are available.'),
    ).toHaveLength(6);
    expect(screen.getAllByText('Unknown').length).toBeGreaterThan(0);
  });

  it('shows unavailable task rates and latency when the report has no observations', () => {
    const noMetrics = structuredClone(artifact);
    noMetrics.summary.baseline_task_success_rate = null;
    noMetrics.summary.baseline_evaluated_repetitions = 0;
    noMetrics.summary.faulted_task_success_rate = null;
    noMetrics.summary.faulted_evaluated_repetitions = 0;
    noMetrics.summary.average_latency_delta_ms = null;
    noMetrics.summary.median_latency_delta_ms = null;
    noMetrics.summary.latency_observations = 0;
    render(<MultiRunInspection artifact={noMetrics} />);
    expect(screen.getByText('Metric unavailable in recorded data.')).toBeVisible();
    expect(screen.getAllByText('Unavailable').length).toBeGreaterThan(0);
  });
});
