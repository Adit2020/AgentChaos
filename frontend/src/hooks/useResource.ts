import { useEffect, useState } from 'react';
import { ApiError } from '../api/client';

type Resource<T> =
  { status: 'loading' } | { status: 'ready'; data: T } | { status: 'error'; error: ApiError };

export function useResource<T>(loader: (signal: AbortSignal) => Promise<T>) {
  const [state, setState] = useState<Resource<T>>({ status: 'loading' });
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setState({ status: 'loading' });
    loader(controller.signal)
      .then((data) => {
        if (active) setState({ status: 'ready', data });
      })
      .catch((error: unknown) => {
        if (active && !controller.signal.aborted)
          setState({
            status: 'error',
            error:
              error instanceof ApiError
                ? error
                : new ApiError('server', 'The request could not be completed.'),
          });
      });
    return () => {
      active = false;
      controller.abort();
    };
  }, [loader, revision]);
  return { state, refresh: () => setRevision((value) => value + 1) };
}
