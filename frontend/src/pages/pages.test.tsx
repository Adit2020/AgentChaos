import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { HistoryPage } from './HistoryPage';
import { DetailPage } from './DetailPage';
import exportsFixture from '../test/fixtures/exports.json';
import multiFixture from '../test/fixtures/multi.json';

describe('history and export states', () => {
  it('shows a loading state while a request is pending', () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => new Promise(() => {})),
    );
    render(<HistoryPage />);
    expect(screen.getByText('Loading experiment history')).toBeVisible();
  });
  it('shows a useful empty state', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"exports":[]}')));
    render(<HistoryPage />);
    expect(await screen.findByText('No recorded experiments yet')).toBeVisible();
  });
  it('reports network and invalid-response failures with retry', async () => {
    const fetch = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('offline'))
      .mockResolvedValueOnce(new Response('{}'));
    vi.stubGlobal('fetch', fetch);
    render(<HistoryPage />);
    expect(await screen.findByText(/Cannot reach the artifacts API/)).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByText(/does not match the supported artifact contract/)).toBeVisible();
  });
  it('lists real response metadata and filters by type and text', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(exportsFixture))));
    render(<HistoryPage />);
    expect(await screen.findByText('2 exports')).toBeVisible();
    await userEvent.selectOptions(screen.getByLabelText('Experiment type'), 'multi_run');
    expect(screen.getByText('1 export')).toBeVisible();
    expect(screen.getByText('Multi-run', { selector: 'span.badge' })).toBeVisible();
    await userEvent.type(screen.getByLabelText('Search experiments'), 'no matching task');
    expect(screen.getByText('No matching experiments')).toBeVisible();
  });
  it('shows a missing-export state', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 404 })));
    render(<DetailPage exportId="missing" />);
    expect(await screen.findByRole('heading', { name: 'Export not found' })).toBeVisible();
  });
  it('loads multi-run reliability analytics from the detail endpoint', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify(multiFixture))));
    render(<DetailPage exportId={multiFixture.export_id} />);
    expect(await screen.findByRole('heading', { name: 'Multi-run reliability' })).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Repetition explorer' })).toBeVisible();
    expect(screen.getByText('Fault-trigger frequency')).toBeVisible();
  });

  it('shows multi-run detail loading and recoverable API errors', async () => {
    const fetch = vi
      .fn()
      .mockImplementationOnce(() => new Promise(() => {}))
      .mockRejectedValueOnce(new TypeError('offline'))
      .mockResolvedValueOnce(new Response(JSON.stringify(multiFixture)));
    vi.stubGlobal('fetch', fetch);
    const view = render(<DetailPage exportId={multiFixture.export_id} />);
    expect(screen.getByText('Loading recorded experiment')).toBeVisible();
    view.unmount();

    render(<DetailPage exportId={multiFixture.export_id} />);
    expect(await screen.findByText(/Cannot reach the artifacts API/)).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByRole('heading', { name: 'Multi-run reliability' })).toBeVisible();
  });
});
