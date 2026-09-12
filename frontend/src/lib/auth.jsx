/**
 * Session state for the console.
 *
 * The profile is re-read from /auth/me on mount rather than decoded from the
 * token: a role change or a deactivation must take effect on the next page
 * load, and a JWT the client decodes itself is a claim, not a fact.
 */

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';

import { api, login as apiLogin, logout as apiLogout, onSessionExpired, tokenStore } from './api.js';

const AuthContext = createContext(null);

// Ordered least to most privileged. Mirrors ROLE_ORDER in
// app/auth/dependencies.py - the server is the enforcement point; this copy
// only decides what the UI bothers to render.
export const ROLE_ORDER = ['VIEWER', 'OPERATOR', 'DEPT_ADMIN', 'SUPER_ADMIN'];

export const ROLE_LABELS = {
  SUPER_ADMIN: 'State Administrator',
  DEPT_ADMIN: 'Department Administrator',
  OPERATOR: 'Control Room Operator',
  VIEWER: 'Viewer',
};

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [status, setStatus] = useState('loading'); // loading | authenticated | anonymous

  // Restore an existing session on first mount.
  useEffect(() => {
    let cancelled = false;

    async function restore() {
      if (!tokenStore.access) {
        setStatus('anonymous');
        return;
      }
      try {
        const profile = await api.get('/api/v1/auth/me');
        if (cancelled) return;
        setUser(profile);
        setStatus('authenticated');
      } catch {
        if (cancelled) return;
        apiLogout();
        setUser(null);
        setStatus('anonymous');
      }
    }

    restore();
    return () => {
      cancelled = true;
    };
  }, []);

  // The API client raises this when the server rejects our token and the
  // refresh also failed; the UI must drop to the login screen immediately
  // rather than keep rendering a shell the user can no longer use.
  useEffect(
    () =>
      onSessionExpired(() => {
        setUser(null);
        setStatus('anonymous');
      }),
    [],
  );

  const signIn = useCallback(async (email, password) => {
    const payload = await apiLogin(email, password);
    setUser(payload.user);
    setStatus('authenticated');
    return payload.user;
  }, []);

  const signOut = useCallback(() => {
    apiLogout();
    setUser(null);
    setStatus('anonymous');
  }, []);

  const value = useMemo(() => {
    const roleIndex = user ? ROLE_ORDER.indexOf(user.role) : -1;
    return {
      user,
      status,
      isAuthenticated: status === 'authenticated',
      signIn,
      signOut,
      /** True when the signed-in user holds `role` or anything above it. */
      atLeast: (role) => roleIndex >= 0 && roleIndex >= ROLE_ORDER.indexOf(role),
      /** The department every request is scoped to, or null for fleet-wide. */
      departmentScope: user?.role === 'SUPER_ADMIN' ? null : user?.department_id ?? null,
    };
  }, [user, status, signIn, signOut]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used inside an AuthProvider');
  }
  return context;
}
