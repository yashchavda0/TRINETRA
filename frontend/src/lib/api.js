/**
 * One HTTP client for the whole console.
 *
 * Every request goes through here so three things are guaranteed in one place:
 * the bearer token is attached, a 401 tears down the session rather than
 * leaving the UI in a half-authenticated state, and errors arrive as a typed
 * ApiError carrying the API's own `detail` message instead of a bare status.
 */

const TOKEN_KEY = 'trinetra.access_token';
const REFRESH_KEY = 'trinetra.refresh_token';

export class ApiError extends Error {
  constructor(status, detail, body) {
    super(detail || `request failed with status ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.body = body;
  }
}

/* ------------------------------------------------------------------ */
/* Token storage                                                       */
/* ------------------------------------------------------------------ */

// sessionStorage rather than localStorage: a shared control-room workstation
// should not keep an operator signed in after the browser closes.
export const tokenStore = {
  get access() {
    try {
      return globalThis.sessionStorage?.getItem(TOKEN_KEY) || null;
    } catch {
      return null;
    }
  },
  get refresh() {
    try {
      return globalThis.sessionStorage?.getItem(REFRESH_KEY) || null;
    } catch {
      return null;
    }
  },
  set({ access_token, refresh_token }) {
    try {
      globalThis.sessionStorage?.setItem(TOKEN_KEY, access_token);
      if (refresh_token) globalThis.sessionStorage?.setItem(REFRESH_KEY, refresh_token);
    } catch {
      // Private mode or blocked storage: the session still works for this tab,
      // it simply will not survive a reload.
    }
  },
  clear() {
    try {
      globalThis.sessionStorage?.removeItem(TOKEN_KEY);
      globalThis.sessionStorage?.removeItem(REFRESH_KEY);
    } catch {
      /* nothing to clear */
    }
  },
};

/* ------------------------------------------------------------------ */
/* Session expiry                                                      */
/* ------------------------------------------------------------------ */

const expiryListeners = new Set();

/** Register a callback invoked when the server rejects our token. */
export function onSessionExpired(listener) {
  expiryListeners.add(listener);
  return () => expiryListeners.delete(listener);
}

function notifyExpired() {
  tokenStore.clear();
  for (const listener of expiryListeners) {
    try {
      listener();
    } catch (error) {
      console.error('session expiry listener failed', error);
    }
  }
}

/* ------------------------------------------------------------------ */
/* Request                                                             */
/* ------------------------------------------------------------------ */

let refreshInFlight = null;

async function attemptRefresh() {
  // Collapse concurrent refreshes: a dashboard firing six queries at once must
  // not send six refresh requests and invalidate its own new token.
  if (refreshInFlight) return refreshInFlight;

  const refresh_token = tokenStore.refresh;
  if (!refresh_token) return null;

  refreshInFlight = (async () => {
    try {
      const response = await fetch('/api/v1/auth/refresh', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token }),
      });
      if (!response.ok) return null;
      const payload = await response.json();
      tokenStore.set(payload);
      return payload.access_token;
    } catch {
      return null;
    } finally {
      // Cleared on the next tick so callers awaiting this promise all see the
      // same result before a new attempt can start.
      setTimeout(() => {
        refreshInFlight = null;
      }, 0);
    }
  })();

  return refreshInFlight;
}

async function parseBody(response) {
  const type = response.headers.get('content-type') || '';
  if (type.includes('application/json')) {
    try {
      return await response.json();
    } catch {
      return null;
    }
  }
  return await response.text();
}

/**
 * Perform an authenticated request.
 *
 * @param {string} path      e.g. '/api/v1/cameras'
 * @param {object} options   fetch options plus `{ json, params, raw }`
 */
export async function request(path, options = {}) {
  const { json, params, raw, headers: extraHeaders, retryOnExpiry = true, ...rest } = options;

  let url = path;
  if (params) {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null || value === '') continue;
      search.append(key, String(value));
    }
    const query = search.toString();
    if (query) url += (url.includes('?') ? '&' : '?') + query;
  }

  const headers = { Accept: 'application/json', ...extraHeaders };
  const token = tokenStore.access;
  if (token) headers.Authorization = `Bearer ${token}`;
  if (json !== undefined) {
    headers['Content-Type'] = 'application/json';
    rest.body = JSON.stringify(json);
  }

  const response = await fetch(url, { ...rest, headers });

  if (response.status === 401 && retryOnExpiry) {
    const fresh = await attemptRefresh();
    if (fresh) {
      return request(path, { ...options, retryOnExpiry: false });
    }
    notifyExpired();
    throw new ApiError(401, 'your session has expired - sign in again');
  }

  if (!response.ok) {
    const body = await parseBody(response);
    const detail =
      (body && typeof body === 'object' && body.detail) ||
      (typeof body === 'string' && body.slice(0, 300)) ||
      null;
    throw new ApiError(response.status, detail, body);
  }

  if (raw) return response;
  if (response.status === 204) return null;
  return parseBody(response);
}

export const api = {
  get: (path, params) => request(path, { method: 'GET', params }),
  post: (path, json) => request(path, { method: 'POST', json }),
  patch: (path, json) => request(path, { method: 'PATCH', json }),
  delete: (path) => request(path, { method: 'DELETE' }),
  raw: (path, options) => request(path, { ...options, raw: true }),
};

/** Login is the one call that must not carry a stale token. */
export async function login(email, password) {
  const response = await fetch('/api/v1/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });

  if (!response.ok) {
    const body = await parseBody(response);
    throw new ApiError(response.status, body?.detail || 'sign-in failed', body);
  }

  const payload = await response.json();
  tokenStore.set(payload);
  return payload;
}

export function logout() {
  tokenStore.clear();
}
