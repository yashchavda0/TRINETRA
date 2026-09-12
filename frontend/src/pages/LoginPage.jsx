import React, { useState } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { ShieldCheck } from 'lucide-react';

import { Button, Field, Input, Spinner } from '../components/ui/index.jsx';
import { useAuth } from '../lib/auth.jsx';

export default function LoginPage() {
  const { signIn, isAuthenticated, status } = useAuth();
  const location = useLocation();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  if (status === 'loading') {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner className="h-6 w-6" />
      </div>
    );
  }

  if (isAuthenticated) {
    // Return the user to whatever they were trying to reach before the
    // session check bounced them here.
    return <Navigate to={location.state?.from?.pathname || '/'} replace />;
  }

  async function handleSubmit(event) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await signIn(email.trim(), password);
    } catch (err) {
      setError(err.detail || err.message || 'Sign-in failed');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full items-center justify-center overflow-y-auto bg-ink-950 px-4 py-10">
      <div className="w-full max-w-sm">
        <div className="mb-8 text-center">
          <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-xl border border-ink-700 bg-ink-850">
            <ShieldCheck className="h-6 w-6 text-accent" aria-hidden />
          </div>
          <h1 className="text-2xl font-bold tracking-[0.2em] text-slate-100">TRINETRA</h1>
          <p className="mt-1 text-sm text-slate-500">Gujarat CCTV Integration Platform</p>
        </div>

        <form
          onSubmit={handleSubmit}
          className="space-y-4 rounded-lg border border-ink-700 bg-ink-850 p-6"
        >
          <Field label="Official email" required>
            <Input
              type="email"
              value={email}
              autoComplete="username"
              autoFocus
              required
              placeholder="operator@gujpolice.gov.in"
              onChange={(event) => setEmail(event.target.value)}
            />
          </Field>

          <Field label="Password" required>
            <Input
              type="password"
              value={password}
              autoComplete="current-password"
              required
              onChange={(event) => setPassword(event.target.value)}
            />
          </Field>

          {error && (
            <p
              role="alert"
              className="rounded-md border border-dept-police/40 bg-dept-police/10 px-3 py-2 text-xs text-dept-police"
            >
              {error}
            </p>
          )}

          <Button type="submit" variant="primary" size="lg" className="w-full" disabled={busy}>
            {busy ? <Spinner className="h-4 w-4 border-white/40 border-t-white" /> : 'Sign in'}
          </Button>
        </form>

        <p className="mt-6 text-center text-[11px] leading-relaxed text-slate-600">
          Authorised users only. Access is scoped to your department and every
          metadata change is recorded in the audit trail.
        </p>
      </div>
    </div>
  );
}
