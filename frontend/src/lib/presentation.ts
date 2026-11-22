import type { RunEvent } from '../api/contracts';

export type Tone = 'neutral' | 'success' | 'danger' | 'warning' | 'accent';
export function verdict(value: boolean | null, recovery = false): { label: string; tone: Tone } {
  if (value === null) return { label: 'Unknown', tone: 'neutral' };
  if (recovery)
    return value
      ? { label: 'Confirmed', tone: 'success' }
      : { label: 'Not recovered', tone: 'danger' };
  return value ? { label: 'Yes', tone: 'success' } : { label: 'No', tone: 'danger' };
}
const eventLabels: Record<RunEvent['kind'], string> = {
  'run.started': 'Run started',
  'run.completed': 'Run completed',
  'run.failed': 'Run failed',
  'run.cancelled': 'Run cancelled',
  'model.request': 'Model request',
  'model.response': 'Model response',
  'model.error': 'Model error',
  'tool.call': 'Tool call',
  'tool.result': 'Tool result',
  'tool.error': 'Tool error',
};
export function eventPresentation(event: RunEvent): { label: string; tone: Tone } {
  if (event.attributes.injected === true)
    return {
      label:
        event.attributes.delivery_confirmed === false
          ? 'Injection delivery unconfirmed'
          : 'Injected tool result',
      tone: 'warning',
    };
  const tone: Tone =
    event.kind.endsWith('error') || event.kind === 'run.failed'
      ? 'danger'
      : event.kind === 'run.completed'
        ? 'success'
        : event.kind === 'run.cancelled'
          ? 'warning'
          : event.kind.startsWith('model')
            ? 'accent'
            : 'neutral';
  return { label: eventLabels[event.kind], tone };
}
export const milliseconds = (value: number | null) =>
  value === null ? 'Unknown' : `${value.toLocaleString()} ms`;
export const signed = (value: number | null, unit = '') =>
  value === null ? 'Unknown' : `${value > 0 ? '+' : ''}${value.toLocaleString()}${unit}`;
export const localDate = (value: string) =>
  new Date(value).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
export const utcTime = (value: string) => `${new Date(value).toISOString().slice(11, 23)} UTC`;
export const faultLabel = (value: string) =>
  ({
    tool_error: 'Tool error',
    tool_timeout: 'Simulated tool timeout',
    malformed_tool_output: 'Malformed tool output',
  })[value] ?? value;
