import { useMemo, useState } from 'react';
import { ArrowUpRight, Search } from 'lucide-react';
import { fetchExports } from '../api/client';
import { useResource } from '../hooks/useResource';
import { faultLabel, localDate } from '../lib/presentation';
import { Badge, PageState, RefreshButton, VerdictBadge } from '../components/ui';

export function HistoryPage() {
  const { state, refresh } = useResource(fetchExports);
  const [search, setSearch] = useState('');
  const [type, setType] = useState('all');
  const exports = state.status === 'ready' ? state.data.exports : [];
  const filtered = useMemo(
    () =>
      exports
        .filter((item) => {
          const experiment = item.manifest.experiment;
          const text = [
            item.export_id,
            experiment.task,
            experiment.fault.target_tool,
            experiment.fault.fault_type,
          ]
            .join(' ')
            .toLowerCase();
          return (
            (type === 'all' || item.artifact_type === type) && text.includes(search.toLowerCase())
          );
        })
        .sort(
          (a, b) =>
            Date.parse(b.exported_at) - Date.parse(a.exported_at) ||
            a.export_id.localeCompare(b.export_id),
        ),
    [exports, search, type],
  );

  return (
    <>
      <div className="page-heading">
        <div>
          <h1>Experiment history</h1>
          <p>Recorded executions, controlled faults, and recovery evidence.</p>
        </div>
        <RefreshButton onClick={refresh} />
      </div>
      {state.status === 'loading' ? (
        <PageState type="loading" title="Loading experiment history" />
      ) : state.status === 'error' ? (
        <PageState type="error" title="Could not load experiments" retry={refresh}>
          {state.error.message}
        </PageState>
      ) : exports.length === 0 ? (
        <PageState type="empty" title="No recorded experiments yet" retry={refresh}>
          Export a baseline/faulted experiment to your configured artifacts directory, then refresh
          to inspect it here.
        </PageState>
      ) : (
        <>
          <div className="history-toolbar">
            <div className="search-field">
              <Search size={15} />
              <input
                aria-label="Search experiments"
                placeholder="Search tasks, exports, or tools…"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
            </div>
            <select
              aria-label="Experiment type"
              value={type}
              onChange={(event) => setType(event.target.value)}
            >
              <option value="all">All experiment types</option>
              <option value="experiment">Single-run</option>
              <option value="multi_run">Multi-run</option>
            </select>
            <span className="result-count">
              {filtered.length} {filtered.length === 1 ? 'export' : 'exports'}
            </span>
          </div>
          {filtered.length === 0 ? (
            <PageState type="empty" title="No matching experiments">
              Try a different task, export ID, or tool name.
            </PageState>
          ) : (
            <div className="table-scroll">
              <p className="table-scroll-hint">Swipe horizontally to see all columns</p>
              <table className="history-table">
                <thead>
                  <tr>
                    <th>Experiment</th>
                    <th>Type</th>
                    <th>Configured fault / tool</th>
                    <th>Recovery</th>
                    <th>Exported · local time</th>
                    <th>
                      <span className="sr-only">Open</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((item) => (
                    <tr key={item.export_id}>
                      <td>
                        <a
                          className="experiment-link"
                          href={`#/exports/${encodeURIComponent(item.export_id)}`}
                        >
                          <strong>{item.manifest.experiment.task ?? 'Task not recorded'}</strong>
                          <code title={item.export_id}>{item.export_id}</code>
                        </a>
                      </td>
                      <td>
                        <Badge tone={item.artifact_type === 'multi_run' ? 'accent' : 'neutral'}>
                          {item.artifact_type === 'experiment' ? 'Single-run' : 'Multi-run'}
                        </Badge>
                      </td>
                      <td>
                        <span>{faultLabel(item.manifest.experiment.fault.fault_type)}</span>
                        <code className="subtext">
                          {item.manifest.experiment.fault.target_tool ?? 'No target tool'}
                        </code>
                      </td>
                      <td>
                        {item.manifest.artifact_type === 'experiment' ? (
                          <VerdictBadge value={item.manifest.recovered} recovery />
                        ) : (
                          <span className="muted">Batch report</span>
                        )}
                      </td>
                      <td>
                        <time dateTime={item.exported_at}>{localDate(item.exported_at)}</time>
                      </td>
                      <td>
                        <a
                          className="icon-link"
                          aria-label={`Open export ${item.export_id}`}
                          href={`#/exports/${encodeURIComponent(item.export_id)}`}
                        >
                          <ArrowUpRight size={17} />
                        </a>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="history-footer">
            <span>Newest exports first</span>
            <span>Single-run comparisons and sequential reliability batches</span>
          </div>
        </>
      )}
    </>
  );
}
