import type { ReactNode } from 'react';
import { AlertTriangle, Database, LoaderCircle, RefreshCw } from 'lucide-react';
import { verdict, type Tone } from '../lib/presentation';

export function Badge({ children, tone = 'neutral' }: { children: ReactNode; tone?: Tone }) {
  return (
    <span className={`badge tone-${tone}`}>
      <span className="status-dot" />
      {children}
    </span>
  );
}
export function VerdictBadge({
  value,
  recovery = false,
}: {
  value: boolean | null;
  recovery?: boolean;
}) {
  const status = verdict(value, recovery);
  return <Badge tone={status.tone}>{status.label}</Badge>;
}
export function MetricCard({
  label,
  value,
  note,
  tone = 'neutral',
}: {
  label: string;
  value: ReactNode;
  note: string;
  tone?: Tone;
}) {
  return (
    <section className={`metric tone-${tone}`}>
      <span className="metric-label">{label}</span>
      <strong>{value}</strong>
      <span className="metric-note">{note}</span>
    </section>
  );
}
export function RefreshButton({
  onClick,
  label = 'Refresh',
}: {
  onClick: () => void;
  label?: string;
}) {
  return (
    <button className="button" onClick={onClick}>
      <RefreshCw size={14} />
      {label}
    </button>
  );
}
export function PageState({
  type,
  title,
  children,
  retry,
}: {
  type: 'loading' | 'empty' | 'error';
  title: string;
  children?: ReactNode;
  retry?: () => void;
}) {
  const Icon = type === 'loading' ? LoaderCircle : type === 'error' ? AlertTriangle : Database;
  return (
    <section className={`page-state ${type}`} role={type === 'error' ? 'alert' : 'status'}>
      <div className="state-icon">
        <Icon size={25} className={type === 'loading' ? 'spinner' : undefined} />
      </div>
      <h2>{title}</h2>
      {children ? <p>{children}</p> : null}
      {retry ? (
        <RefreshButton onClick={retry} label={type === 'error' ? 'Try again' : 'Refresh'} />
      ) : null}
    </section>
  );
}
