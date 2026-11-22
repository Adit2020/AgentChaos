import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { detailSchema } from '../api/contracts';
import { eventPresentation, verdict } from '../lib/presentation';
import { EventTimeline, RunOutput } from './EventTimeline';
import { SingleInspection } from '../pages/DetailPage';
import singleFixture from '../test/fixtures/single.json';

const detail = detailSchema.parse(singleFixture);
if (detail.artifact_type !== 'experiment') throw new Error('Wrong fixture type');
const artifact = detail.artifact;

describe('evidence interpretation', () => {
  it('keeps confirmed, negative, and unknown recovery distinct', () => {
    expect(verdict(true, true)).toEqual({ label: 'Confirmed', tone: 'success' });
    expect(verdict(false, true)).toEqual({ label: 'Not recovered', tone: 'danger' });
    expect(verdict(null, true)).toEqual({ label: 'Unknown', tone: 'neutral' });
    render(<SingleInspection artifact={artifact} />);
    expect(screen.getByText('Insufficient evidence for a verdict')).toBeVisible();
    expect(screen.queryByText('Confirmed')).not.toBeInTheDocument();
  });
  it('summarizes separate evidence and navigates to the uniquely marked injected event', () => {
    const previousScrollIntoView = HTMLElement.prototype.scrollIntoView;
    const scrollIntoView = vi.fn();
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value: scrollIntoView,
    });
    try {
      const { container } = render(<SingleInspection artifact={artifact} />);
      const outcome = screen.getByRole('region', { name: 'Execution outcome' });
      expect(within(outcome).getByText('Configured fault triggered')).toBeVisible();
      expect(within(outcome).getByText('Agent continued')).toBeVisible();
      expect(within(outcome).getByText('Task success')).toBeVisible();
      expect(within(outcome).getByText('Observed execution cost')).toBeVisible();
      expect(within(outcome).getByText(/no monetary cost is recorded/)).toBeVisible();

      fireEvent.click(within(outcome).getByRole('button', { name: 'View injected event' }));
      const injected = container.querySelector(
        `[data-event-id="${artifact.faulted.events[2].event_id}"]`,
      );
      expect(injected).toHaveClass('injected');
      expect(injected?.querySelector('button')).toHaveAttribute('aria-expanded', 'true');
      expect(scrollIntoView).toHaveBeenCalledWith({ block: 'center' });
    } finally {
      if (previousScrollIntoView) {
        Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
          configurable: true,
          value: previousScrollIntoView,
        });
      } else {
        delete (HTMLElement.prototype as Partial<HTMLElement>).scrollIntoView;
      }
    }
  });
  it('hides injected-event navigation when the marker is missing', () => {
    const missingEvidence = structuredClone(artifact);
    const event = missingEvidence.faulted.events.find(
      (entry) => entry.attributes.injected === true,
    );
    if (event) delete event.attributes.injected;
    render(<SingleInspection artifact={missingEvidence} />);
    expect(screen.queryByRole('button', { name: 'View injected event' })).not.toBeInTheDocument();
  });
  it('orders events and limits call correlation to the individual run', () => {
    const run = { ...artifact.baseline, events: [...artifact.baseline.events].reverse() };
    const { container } = render(
      <>
        <EventTimeline run={run} title="Baseline" duration={1000} />
        <EventTimeline run={artifact.faulted} title="Faulted" duration={1000} />
      </>,
    );
    const baseline = screen.getByRole('region', { name: 'Baseline execution trace' });
    expect(
      within(baseline)
        .getAllByRole('button')
        .map((button) => button.textContent?.slice(0, 2)),
    ).toEqual(['00', '01', '02', '03']);
    fireEvent.click(within(baseline).getByRole('button', { name: /Tool call/ }));
    expect(within(baseline).getByText(/related events in this run:/)).toHaveTextContent('#1, #2');
    expect(baseline.querySelectorAll('[data-related]')).toHaveLength(2);
    expect(container.querySelectorAll('.event-inspector')).toHaveLength(2);
  });
  it('marks injected results and explains simulated timeout metadata', () => {
    const run = structuredClone(artifact.faulted);
    run.events[2].attributes.timeout_simulated = true;
    run.events[2].attributes.fault_kind = 'tool_timeout';
    expect(eventPresentation(run.events[2]).tone).toBe('warning');
    render(<EventTimeline run={run} title="Faulted" duration={1000} />);
    expect(screen.getByText('Injected tool result')).toBeVisible();
    expect(screen.getByText(/Simulated tool-boundary timeout/)).toBeVisible();
  });
  it('renders output as text, never executable HTML', () => {
    const output = '<script>alert("artifact")</script>';
    const { container } = render(
      <RunOutput run={{ ...artifact.baseline, output }} title="Baseline" />,
    );
    expect(screen.getByText(output)).toBeVisible();
    expect(container.querySelector('script')).toBeNull();
  });
});
