import { useEffect, useSyncExternalStore } from 'react';
import { CodeXml, FlaskConical, LockKeyhole, Monitor, Play } from 'lucide-react';
import { HistoryPage } from './pages/HistoryPage';
import { DetailPage } from './pages/DetailPage';
import { PageState } from './components/ui';
import { PlaygroundPage } from './pages/PlaygroundPage';

const subscribe = (notify: () => void) => {
  window.addEventListener('hashchange', notify);
  return () => window.removeEventListener('hashchange', notify);
};
const snapshot = () => window.location.hash;

export default function App() {
  const hash = useSyncExternalStore(subscribe, snapshot);
  let id: string | null = null;
  let jobId: string | undefined;
  const playground = hash === '#/playground' || hash.startsWith('#/playground/jobs/');
  let invalid = false;
  if (hash && hash !== '#/' && hash !== '#') {
    if (hash.startsWith('#/exports/')) {
      try {
        id = decodeURIComponent(hash.slice('#/exports/'.length));
        invalid = !id || /[/\\]/.test(id);
      } catch {
        invalid = true;
      }
    } else if (hash.startsWith('#/playground/jobs/')) {
      try {
        jobId = decodeURIComponent(hash.slice('#/playground/jobs/'.length));
        invalid = !jobId || /[/\\]/.test(jobId);
      } catch {
        invalid = true;
      }
    } else if (hash !== '#/playground') invalid = true;
  }
  useEffect(() => {
    document.title = playground
      ? 'Experiment Playground · AgentChaos Studio'
      : id
        ? 'Experiment inspection · AgentChaos Studio'
        : 'Experiment history · AgentChaos Studio';
    window.scrollTo(0, 0);
  }, [id, playground, jobId]);
  return (
    <div className="app-shell">
      <a
        className="skip-link"
        href="#main-content"
        onClick={(event) => {
          event.preventDefault();
          document.getElementById('main-content')?.focus();
        }}
      >
        Skip to content
      </a>
      <aside className="sidebar">
        <a className="brand" href="#/" aria-label="AgentChaos Studio home">
          <CodeXml size={31} />
          <span>
            AgentChaos<small>Studio</small>
          </span>
        </a>
        <nav aria-label="Main navigation">
          <a
            className={`nav-item ${!playground ? 'active' : ''}`}
            href="#/"
            aria-current={!playground && !id && !invalid ? 'page' : undefined}
          >
            <FlaskConical size={16} />
            Experiments
          </a>
          <a
            className={`nav-item ${playground ? 'active' : ''}`}
            href="#/playground"
            aria-current={playground && !invalid ? 'page' : undefined}
          >
            <Play size={16} />
            Playground
          </a>
        </nav>
        <div className="workspace-note">
          <Monitor size={15} />
          <span>
            Local workspace<small>{playground ? 'Trusted scenarios' : 'Artifact inspection'}</small>
          </span>
        </div>
      </aside>
      <div className="workspace">
        <header className="app-header">
          <div className="breadcrumbs">
            <a href={playground ? '#/playground' : '#/'}>
              {playground ? 'Playground' : 'Experiments'}
            </a>
            {jobId ? (
              <>
                <span>/</span>
                <span>Job</span>
              </>
            ) : null}
            {id ? (
              <>
                <span>/</span>
                <span>Inspection</span>
              </>
            ) : null}
          </div>
          <span className="read-only">
            {playground ? (
              <>
                <Play size={12} />
                Live execution
              </>
            ) : (
              <>
                <LockKeyhole size={12} />
                Read-only
              </>
            )}
          </span>
        </header>
        <main id="main-content" tabIndex={-1}>
          {invalid ? (
            <PageState type="error" title="Page not found">
              Use Experiment history to select a recorded export.
            </PageState>
          ) : playground ? (
            <PlaygroundPage key={jobId ?? 'configure'} jobId={jobId} />
          ) : id ? (
            <DetailPage key={id} exportId={id} />
          ) : (
            <HistoryPage />
          )}
        </main>
        <footer className="app-footer">
          <span>AgentChaos Studio</span>
          <span>Evidence before assumptions.</span>
        </footer>
      </div>
    </div>
  );
}
