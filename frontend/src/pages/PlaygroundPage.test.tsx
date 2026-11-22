import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { PlaygroundPage } from './PlaygroundPage';
import App from '../App';
import type { JobStatus, Scenario } from '../api/contracts';
import single from '../test/fixtures/single.json';

const scenarios: Scenario[] = [
  {
    scenario_id: 'order_support',
    name: 'Order support',
    description: 'A trusted local order lookup.',
    supported_target_tools: ['get_order_status_json'],
    supported_fault_types: ['tool_error', 'tool_timeout', 'malformed_tool_output'],
    default_task: 'Where is order 123?',
    max_occurrence: 3,
  },
  {
    scenario_id: 'inventory_lab',
    name: 'Inventory lab',
    description: 'Another registered scenario.',
    supported_target_tools: ['lookup_stock', 'lookup_backup'],
    supported_fault_types: ['tool_timeout'],
    default_task: 'Check fixed inventory.',
    max_occurrence: 2,
  },
];
const initial: JobStatus = {
  job_id: 'offline-job',
  scenario_id: 'order_support',
  state: 'queued',
  submitted_at: '2026-11-08T12:00:00Z',
  started_at: null,
  finished_at: null,
  export_id: null,
  partial_export: false,
  cancellation_pending: false,
  error: null,
};
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
const flush = async () => {
  await act(async () => {
    await Promise.resolve();
  });
};
beforeEach(() => {
  window.location.hash = '#/playground';
});
afterEach(() => vi.useRealTimers());

describe('Playground configuration and submission', () => {
  it('loads only server-supported options and never submits during discovery', async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(json({ scenarios })));
    vi.stubGlobal('fetch', fetch);
    render(<PlaygroundPage />);
    expect(screen.getByText('Loading registered scenarios')).toBeVisible();
    expect(await screen.findByText('Where is order 123?')).toBeVisible();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('checkbox'));
    await userEvent.selectOptions(screen.getByLabelText('Registered scenario'), 'inventory_lab');
    expect(screen.getByText('Check fixed inventory.')).toBeVisible();
    expect(screen.getByLabelText('Fault type').querySelectorAll('option')).toHaveLength(1);
    expect(screen.getByLabelText('Target tool').querySelectorAll('option')).toHaveLength(2);
    expect(screen.getByLabelText('Target occurrence')).toHaveAttribute('max', '2');
    expect(screen.getByRole('checkbox')).not.toBeChecked();
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it('requires acknowledgment and rejects an unsupported occurrence', async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(json({ scenarios })));
    vi.stubGlobal('fetch', fetch);
    render(<PlaygroundPage />);
    await screen.findByText('Where is order 123?');
    await userEvent.click(screen.getByRole('button', { name: 'Run Experiment' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Acknowledge');
    fireEvent.change(screen.getByLabelText('Target occurrence'), { target: { value: '4' } });
    await userEvent.click(screen.getByRole('checkbox'));
    await userEvent.click(screen.getByRole('button', { name: 'Run Experiment' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Choose a supported');
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it('submits exact configuration once and retains the accepted job in the URL', async () => {
    let accept!: (value: Response) => void;
    const fetch = vi.fn().mockImplementation((_path: string, init?: RequestInit) =>
      init?.method === 'POST'
        ? new Promise<Response>((resolve) => {
            accept = resolve;
          })
        : Promise.resolve(json({ scenarios })),
    );
    vi.stubGlobal('fetch', fetch);
    render(<PlaygroundPage />);
    await screen.findByText('Where is order 123?');
    await userEvent.click(screen.getByRole('checkbox'));
    const form = screen.getByRole('button', { name: 'Run Experiment' }).closest('form')!;
    fireEvent.submit(form);
    fireEvent.submit(form);
    expect(screen.getByRole('button', { name: 'Submitting…' })).toBeDisabled();
    const posts = fetch.mock.calls.filter((call) => call[1]?.method === 'POST');
    expect(posts).toHaveLength(1);
    expect(posts[0][0]).toBe('/api/jobs');
    expect(JSON.parse(posts[0][1].body)).toEqual({
      scenario_id: 'order_support',
      fault_type: 'tool_error',
      target_tool: 'get_order_status_json',
      occurrence: 1,
      acknowledge_live_cost: true,
    });
    expect(posts[0][1].headers).toEqual({ 'Content-Type': 'application/json' });
    expect(posts[0][1].credentials).toBe('omit');
    await act(async () => accept(json(initial, 202)));
    expect(window.location.hash).toBe('#/playground/jobs/offline-job');
  });
  it.each([429, 422, 403])(
    'handles HTTP %s without displaying arbitrary server error text',
    async (status) => {
      const fetch = vi
        .fn()
        .mockImplementation((_path: string, init?: RequestInit) =>
          Promise.resolve(
            init?.method === 'POST'
              ? json({ error: { message: 'sk-secret /private/path' } }, status)
              : json({ scenarios }),
          ),
        );
      vi.stubGlobal('fetch', fetch);
      render(<PlaygroundPage />);
      await screen.findByText('Where is order 123?');
      await userEvent.click(screen.getByRole('checkbox'));
      await userEvent.click(screen.getByRole('button', { name: 'Run Experiment' }));
      const alert = await screen.findByRole('alert');
      expect(alert).not.toHaveTextContent('sk-secret');
      expect(alert).toHaveTextContent(
        status === 429
          ? 'Another experiment'
          : status === 422
            ? 'no longer supported'
            : 'security checks',
      );
      expect(screen.getByRole('checkbox')).not.toBeChecked();
      expect(fetch.mock.calls.filter((call) => call[1]?.method === 'POST')).toHaveLength(1);
    },
  );
  it('reports uncertain submission after a network failure without retrying POST', async () => {
    const fetch = vi
      .fn()
      .mockImplementation((_path: string, init?: RequestInit) =>
        init?.method === 'POST'
          ? Promise.reject(new TypeError('offline'))
          : Promise.resolve(json({ scenarios })),
      );
    vi.stubGlobal('fetch', fetch);
    render(<PlaygroundPage />);
    await screen.findByText('Where is order 123?');
    await userEvent.click(screen.getByRole('checkbox'));
    await userEvent.click(screen.getByRole('button', { name: 'Run Experiment' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('server may have accepted');
    expect(screen.getByRole('link', { name: 'experiment history' })).toHaveAttribute('href', '#/');
    expect(fetch).toHaveBeenCalledTimes(2);
  });
  it('shows empty, offline, and malformed registry states', async () => {
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('offline'))
      .mockResolvedValueOnce(json({ scenarios: null }))
      .mockResolvedValueOnce(json({ scenarios: [] }));
    vi.stubGlobal('fetch', fetch);
    render(<PlaygroundPage />);
    expect(await screen.findByText(/Cannot reach the execution API/)).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByText(/supported execution contract/)).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByText('No scenarios registered')).toBeVisible();
  });
});

describe('job polling and evidence', () => {
  it('ignores a late response after unmount and creates no new polling timer', async () => {
    vi.useFakeTimers();
    let finish!: (response: Response) => void;
    const fetch = vi.fn().mockImplementation(
      () =>
        new Promise<Response>((resolve) => {
          finish = resolve;
        }),
    );
    vi.stubGlobal('fetch', fetch);
    const view = render(<PlaygroundPage jobId="offline-job" />);
    view.unmount();
    expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
    await act(async () => finish(json(initial)));
    await act(async () => vi.advanceTimersByTimeAsync(6000));
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it('polls queued and running states every two seconds and stops on completion', async () => {
    vi.useFakeTimers();
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json(initial))
      .mockResolvedValueOnce(
        json({ ...initial, state: 'running', started_at: initial.submitted_at }),
      )
      .mockResolvedValueOnce(
        json({ ...initial, state: 'completed', finished_at: initial.submitted_at }),
      );
    vi.stubGlobal('fetch', fetch);
    render(<PlaygroundPage jobId="offline-job" />);
    await flush();
    expect(screen.getByText('Queued', { selector: '.badge' })).toBeVisible();
    await act(async () => vi.advanceTimersByTimeAsync(1999));
    expect(fetch).toHaveBeenCalledTimes(1);
    await act(async () => vi.advanceTimersByTimeAsync(1));
    expect(screen.getByText('Running', { selector: '.badge' })).toBeVisible();
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(screen.getByText('Completed', { selector: '.badge' })).toBeVisible();
    await act(async () => vi.advanceTimersByTimeAsync(10000));
    expect(fetch).toHaveBeenCalledTimes(3);
  });
  it('avoids overlapping requests and aborts a pending check on unmount', async () => {
    vi.useFakeTimers();
    let resolve!: (value: Response) => void;
    const fetch = vi.fn().mockImplementation(
      () =>
        new Promise<Response>((done) => {
          resolve = done;
        }),
    );
    vi.stubGlobal('fetch', fetch);
    const view = render(<PlaygroundPage jobId="offline-job" />);
    await act(async () => vi.advanceTimersByTimeAsync(6000));
    expect(fetch).toHaveBeenCalledTimes(1);
    await act(async () => resolve(json(initial)));
    view.unmount();
    expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
    await act(async () => vi.advanceTimersByTimeAsync(6000));
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it('retries temporary network failure while retaining the last reported state', async () => {
    vi.useFakeTimers();
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(json({ ...initial, state: 'running' }))
      .mockRejectedValueOnce(new TypeError('offline'))
      .mockResolvedValueOnce(
        json({
          ...initial,
          state: 'failed',
          error: { code: 'execution_failed', message: 'sk-secret' },
        }),
      );
    vi.stubGlobal('fetch', fetch);
    render(<PlaygroundPage jobId="offline-job" />);
    await flush();
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(screen.getByRole('alert')).toHaveTextContent('Last reported status');
    expect(screen.getByText('Running', { selector: '.badge' })).toBeVisible();
    await act(async () => vi.advanceTimersByTimeAsync(2000));
    expect(screen.getByText('Failed', { selector: '.badge' })).toBeVisible();
    expect(screen.queryByText('sk-secret')).not.toBeInTheDocument();
    await act(async () => vi.advanceTimersByTimeAsync(6000));
    expect(fetch).toHaveBeenCalledTimes(3);
  });
  it('stops on a timed-out state even with cancellation pending and allows manual checking', async () => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockImplementation(() =>
      Promise.resolve(
        json({
          ...initial,
          state: 'timed_out',
          cancellation_pending: true,
          error: { code: 'execution_timeout', message: 'deadline' },
        }),
      ),
    );
    vi.stubGlobal('fetch', fetch);
    render(<PlaygroundPage jobId="offline-job" />);
    await flush();
    expect(screen.getByText('Cancellation is still pending')).toBeVisible();
    await act(async () => vi.advanceTimersByTimeAsync(10000));
    expect(fetch).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Check status' }));
    await flush();
    expect(fetch).toHaveBeenCalledTimes(2);
  });
  it.each([404, 200])('handles a missing or malformed job response (%s)', async (status) => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(json({}, status)));
    vi.stubGlobal('fetch', fetch);
    render(<PlaygroundPage jobId="offline-job" />);
    await flush();
    expect(screen.getByRole('alert')).toHaveTextContent(
      status === 404 ? 'stored in memory' : 'supported execution contract',
    );
    await act(async () => vi.advanceTimersByTimeAsync(10000));
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it.each([true, false])('only links readable partial evidence (readable=%s)', async (readable) => {
    const fetch = vi.fn().mockImplementation((path: string) =>
      Promise.resolve(
        path.startsWith('/api/jobs/')
          ? json({
              ...initial,
              state: 'failed',
              partial_export: true,
              export_id: single.export_id,
            })
          : json(readable ? single : {}, readable ? 200 : 404),
      ),
    );
    vi.stubGlobal('fetch', fetch);
    render(<PlaygroundPage jobId="offline-job" />);
    if (readable) {
      expect(await screen.findByRole('link', { name: 'View partial evidence' })).toHaveAttribute(
        'href',
        `#/exports/${single.export_id}`,
      );
      expect(screen.getByText(/contains partial execution evidence/)).toBeVisible();
    } else {
      expect(await screen.findByText('Saved evidence cannot currently be opened.')).toBeVisible();
      expect(screen.queryByRole('link', { name: 'View partial evidence' })).not.toBeInTheDocument();
    }
    expect(screen.queryByRole('link', { name: 'View Results' })).not.toBeInTheDocument();
  });
  it('recovers a job URL and opens the existing inspection page using real artifact loading', async () => {
    window.location.hash = '#/playground/jobs/offline-job';
    const fetch = vi.fn().mockImplementation((path: string) =>
      Promise.resolve(
        path.startsWith('/api/jobs/')
          ? json({
              ...initial,
              state: 'completed',
              export_id: single.export_id,
              finished_at: initial.submitted_at,
            })
          : json(single),
      ),
    );
    vi.stubGlobal('fetch', fetch);
    render(<App />);
    const results = await screen.findByRole('link', { name: 'View Results' });
    expect(screen.getByRole('link', { name: 'Playground', current: 'page' })).toBeVisible();
    expect(fetch.mock.calls.some((call) => call[0] === '/api/scenarios')).toBe(false);
    await userEvent.click(results);
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: 'Experiment inspection' })).toBeVisible(),
    );
    expect(window.location.hash).toBe(`#/exports/${single.export_id}`);
    expect(fetch.mock.calls.some((call) => call[1]?.method === 'POST')).toBe(false);
  });
});
