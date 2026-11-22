import { useEffect, useRef, useState } from 'react';
import { ApiError, fetchJob } from '../api/client';
import type { JobStatus } from '../api/contracts';

export const isActiveJob = (job: JobStatus) => job.state === 'queued' || job.state === 'running';

export function useJob(jobId: string) {
  const [job, setJob] = useState<JobStatus | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [checking, setChecking] = useState(true);
  const [revision, setRevision] = useState(0);
  const terminal = useRef(false);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    async function check() {
      setChecking(true);
      let again = false;
      try {
        const data = await fetchJob(jobId, controller.signal);
        if (!active) return;
        setJob(data);
        setError(null);
        terminal.current = !isActiveJob(data);
        again = isActiveJob(data);
      } catch (caught) {
        if (!active) return;
        const failure =
          caught instanceof ApiError
            ? caught
            : new ApiError('server', 'Job status could not be checked.');
        setError(failure);
        again =
          !terminal.current &&
          (failure.kind === 'network' || failure.kind === 'server' || failure.kind === 'busy');
      } finally {
        if (active) {
          setChecking(false);
          if (again) timer = setTimeout(check, 2000);
        }
      }
    }
    void check();
    return () => {
      active = false;
      controller.abort();
      clearTimeout(timer);
    };
  }, [jobId, revision]);
  return { job, error, checking, refresh: () => setRevision((value) => value + 1) };
}
