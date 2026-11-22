import { describe, expect, it, vi } from 'vitest';
import { detailSchema, exportListSchema } from './contracts';
import { fetchExport, fetchExports, fetchJob, submitJob } from './client';
import exportsFixture from '../test/fixtures/exports.json';
import singleFixture from '../test/fixtures/single.json';
import multiFixture from '../test/fixtures/multi.json';

describe('actual API contracts', () => {
  it('accepts complete responses produced through the writer, readers, and API', () => {
    expect(exportListSchema.parse(exportsFixture).exports).toHaveLength(2);
    expect(detailSchema.parse(singleFixture).artifact_type).toBe('experiment');
    expect(detailSchema.parse(multiFixture).artifact_type).toBe('multi_run');
  });
  it('rejects a missing metric rather than inventing a value', () => {
    const fixture = structuredClone(singleFixture);
    const { continued_after_fault: _removed, ...comparison } = fixture.artifact.comparison;
    expect(
      detailSchema.safeParse({ ...fixture, artifact: { ...fixture.artifact, comparison } }).success,
    ).toBe(false);
  });
  it('encodes export IDs and calls the actual endpoint', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(singleFixture)));
    vi.stubGlobal('fetch', fetch);
    await fetchExport(singleFixture.export_id);
    expect(fetch.mock.calls[0][0]).toBe(
      `/api/exports/${encodeURIComponent(singleFixture.export_id)}`,
    );
  });
  it('distinguishes network, missing export, and invalid response errors', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('network')));
    await expect(fetchExports()).rejects.toMatchObject({ kind: 'network' });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 404 })));
    await expect(fetchExport('missing')).rejects.toMatchObject({ kind: 'missing' });
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"exports":null}')));
    await expect(fetchExports()).rejects.toMatchObject({ kind: 'invalid' });
  });
  it('rejects a response for a different requested export', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(singleFixture))));
    await expect(fetchExport('different-id')).rejects.toMatchObject({ kind: 'invalid' });
  });
});

describe('execution response validation', () => {
  const payload = {
    scenario_id: 'order_support',
    fault_type: 'tool_error' as const,
    target_tool: 'get_order_status_json',
    occurrence: 1,
    acknowledge_live_cost: true as const,
  };
  it('marks malformed acceptance as uncertain and never retries submission', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response('{}', { status: 202 }));
    vi.stubGlobal('fetch', fetch);
    await expect(submitJob(payload)).rejects.toMatchObject({
      kind: 'invalid',
      submissionUncertain: true,
    });
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it('rejects an unacknowledged request before fetching', async () => {
    const fetch = vi.fn();
    vi.stubGlobal('fetch', fetch);
    await expect(
      submitJob({ ...payload, acknowledge_live_cost: false } as unknown as typeof payload),
    ).rejects.toThrow();
    expect(fetch).not.toHaveBeenCalled();
  });
  it('rejects a different returned job ID', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            job_id: 'other',
            scenario_id: 'order_support',
            state: 'running',
            submitted_at: '2026-11-08T12:00:00Z',
            started_at: null,
            finished_at: null,
            export_id: null,
            partial_export: false,
            cancellation_pending: false,
            error: null,
          }),
        ),
      ),
    );
    await expect(fetchJob('expected')).rejects.toMatchObject({ kind: 'invalid' });
  });
});
