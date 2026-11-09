import type { Page } from './types';

let csrfToken = '';
export const setCsrfToken = (value: string) => { csrfToken = value; };

export class ApiError extends Error {
  constructor(public code: string, message: string, public correlationId: string | null = null, public status = 0) { super(message); }
}

export async function api<T>(path: string, options: { body?: unknown; key?: string; method?: 'GET' | 'POST'; signal?: AbortSignal } = {}): Promise<T> {
  const method = options.method ?? 'GET';
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (method === 'POST') {
    if (!csrfToken) throw new ApiError('SESSION_NOT_READY', 'The application session is not ready. Reload this page.');
    headers['X-CSRF-Token'] = csrfToken;
    if (options.key) headers['Idempotency-Key'] = options.key;
    if (options.body !== undefined) headers['Content-Type'] = 'application/json';
  }
  let response: Response;
  try {
    response = await fetch(`/api/v1${path}`, { method, headers, body: options.body === undefined ? undefined : JSON.stringify(options.body), credentials: 'same-origin', cache: 'no-store', signal: options.signal });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError('CONNECTION_FAILED', 'The local API could not be reached. The last displayed records have not been refreshed.');
  }
  let value: any;
  try { value = await response.json(); } catch { throw new ApiError('INVALID_RESPONSE', `The server returned a non-JSON response (${response.status}).`, null, response.status); }
  if (!response.ok) throw new ApiError(value.error?.code ?? 'REQUEST_FAILED', value.error?.message ?? `Request failed (${response.status}).`, value.error?.correlation_id ?? null, response.status);
  if (value.schema_version !== undefined && value.schema_version !== 'autotriager.api.v1') throw new ApiError('UNSUPPORTED_CONTRACT', 'The server returned an unsupported contract version.');
  return value as T;
}

export const pageUrl = (path: string, cursor?: string | null) => `${path}?limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`;
export const getPage = <T>(path: string, cursor?: string | null) => api<Page<T>>(pageUrl(path, cursor));
export const id = encodeURIComponent;
export const errorText = (error: unknown) => error instanceof ApiError ? `${error.code}: ${error.message}${error.correlationId ? ` (${error.correlationId})` : ''}` : error instanceof Error ? error.message : String(error);
