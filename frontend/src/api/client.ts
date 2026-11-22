import { z } from 'zod';
import {
  detailSchema,
  exportListSchema,
  scenarioListSchema,
  jobStatusSchema,
  jobRequestSchema,
  type JobRequest,
} from './contracts';

export class ApiError extends Error {
  constructor(
    public kind: 'network' | 'missing' | 'invalid' | 'server' | 'busy',
    message: string,
    public submissionUncertain = false,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function request<T>(
  path: string,
  schema: z.ZodType<T>,
  signal?: AbortSignal,
  init: RequestInit = {},
  scope: 'artifact' | 'job' | 'scenario' = 'artifact',
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { ...init, signal, cache: 'no-store', credentials: 'omit' });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new ApiError(
      'network',
      scope === 'artifact'
        ? 'Cannot reach the artifacts API. Check that the backend is running on port 8000.'
        : 'Cannot reach the execution API. Check that the backend is running on port 8000.',
      init.method === 'POST',
    );
  }
  if (!response.ok) {
    if (scope !== 'artifact') {
      if (response.status === 429)
        throw new ApiError(
          'busy',
          'Another experiment is already running or finishing. Wait for it to stop before trying again.',
        );
      if (response.status === 404)
        throw new ApiError(
          'missing',
          scope === 'job'
            ? 'This job is no longer available. Job state is stored in memory and can be lost after a server restart or retention expiry.'
            : 'The scenario registry is unavailable. Check that the backend supports experiment execution.',
        );
      if (response.status === 422)
        throw new ApiError(
          'invalid',
          'The selected scenario or fault configuration is no longer supported. Reload scenarios and review the form.',
        );
      if ([400, 403, 415].includes(response.status))
        throw new ApiError(
          'server',
          'Submission rejected by local API security checks. Open Studio on localhost through its /api proxy.',
        );
      throw new ApiError(
        'server',
        'The execution API could not complete the request. Check that the backend is available.',
        init.method === 'POST',
      );
    }
    if (response.status === 404)
      throw new ApiError('missing', 'This export or one of its required files could not be found.');
    if (response.status === 409)
      throw new ApiError(
        'server',
        'This export ID is ambiguous. The API cannot select a unique export.',
      );
    if (response.status === 422)
      throw new ApiError(
        'invalid',
        'The API could not read this artifact. Its data may be invalid or its schema unsupported.',
      );
    throw new ApiError('server', 'The artifacts API could not complete the request.');
  }
  if (init.method === 'POST' && response.status !== 202)
    throw new ApiError('invalid', 'The API did not confirm job acceptance.', true);
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    throw new ApiError('invalid', 'The API returned an invalid response.', init.method === 'POST');
  }
  const parsed = schema.safeParse(data);
  if (!parsed.success)
    throw new ApiError(
      'invalid',
      scope === 'artifact'
        ? 'The API response does not match the supported artifact contract.'
        : 'The API response does not match the supported execution contract.',
      init.method === 'POST',
    );
  return parsed.data;
}

export const fetchScenarios = (signal?: AbortSignal) =>
  request('/api/scenarios', scenarioListSchema, signal, {}, 'scenario');
export async function submitJob(payload: JobRequest, signal?: AbortSignal) {
  const validated = jobRequestSchema.parse(payload);
  const job = await request(
    '/api/jobs',
    jobStatusSchema,
    signal,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(validated),
    },
    'job',
  );
  if (job.scenario_id !== payload.scenario_id)
    throw new ApiError('invalid', 'The API returned a different scenario than requested.', true);
  return job;
}
export async function fetchJob(id: string, signal?: AbortSignal) {
  const job = await request(
    `/api/jobs/${encodeURIComponent(id)}`,
    jobStatusSchema,
    signal,
    {},
    'job',
  );
  if (job.job_id !== id)
    throw new ApiError('invalid', 'The API returned a different job than requested.');
  return job;
}

export const fetchExports = (signal?: AbortSignal) =>
  request('/api/exports', exportListSchema, signal);
export async function fetchExport(id: string, signal?: AbortSignal) {
  const data = await request(`/api/exports/${encodeURIComponent(id)}`, detailSchema, signal);
  if (data.export_id !== id)
    throw new ApiError('invalid', 'The API returned a different export than requested.');
  return data;
}
