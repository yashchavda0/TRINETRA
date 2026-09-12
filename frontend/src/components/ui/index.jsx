/**
 * Shared UI primitives.
 *
 * Deliberately hand-written rather than pulled from a component library: the
 * console needs about a dozen elements, all of them dark-surface variants, and
 * a full design system would be more configuration than code. Everything here
 * is a thin wrapper over a native element so forms, focus and accessibility
 * behave the way the browser already knows how to.
 */

import React from 'react';
import { clsx } from 'clsx';
import { twMerge } from 'tailwind-merge';

/** Merge conditional classes, with later Tailwind utilities winning. */
export function cn(...inputs) {
  return twMerge(clsx(inputs));
}

/* ------------------------------------------------------------------ */
/* Button                                                              */
/* ------------------------------------------------------------------ */

const BUTTON_VARIANTS = {
  primary: 'bg-accent hover:bg-accent-soft text-white border-transparent',
  secondary: 'bg-ink-800 hover:bg-ink-700 text-slate-200 border-ink-700',
  ghost: 'bg-transparent hover:bg-ink-800 text-slate-300 border-transparent',
  danger: 'bg-dept-police/90 hover:bg-dept-police text-white border-transparent',
  outline: 'bg-transparent hover:bg-ink-800 text-slate-200 border-ink-600',
};

const BUTTON_SIZES = {
  sm: 'h-8 px-3 text-xs gap-1.5',
  md: 'h-9 px-4 text-sm gap-2',
  lg: 'h-11 px-6 text-sm gap-2',
  icon: 'h-9 w-9 p-0 justify-center',
};

export const Button = React.forwardRef(function Button(
  { variant = 'secondary', size = 'md', className, type = 'button', ...props },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={cn(
        'inline-flex items-center justify-center rounded-md border font-medium transition-colors',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-ink-900',
        'disabled:opacity-50 disabled:pointer-events-none whitespace-nowrap',
        BUTTON_VARIANTS[variant],
        BUTTON_SIZES[size],
        className,
      )}
      {...props}
    />
  );
});

/* ------------------------------------------------------------------ */
/* Form controls                                                       */
/* ------------------------------------------------------------------ */

export const Input = React.forwardRef(function Input({ className, invalid, ...props }, ref) {
  return (
    <input
      ref={ref}
      aria-invalid={invalid || undefined}
      className={cn(
        'h-9 w-full rounded-md border bg-ink-950/60 px-3 text-sm text-slate-100 placeholder:text-slate-500',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        'disabled:opacity-50',
        invalid ? 'border-dept-police' : 'border-ink-700',
        className,
      )}
      {...props}
    />
  );
});

export const Select = React.forwardRef(function Select({ className, children, ...props }, ref) {
  return (
    <select
      ref={ref}
      className={cn(
        'h-9 w-full rounded-md border border-ink-700 bg-ink-950/60 px-2 text-sm text-slate-100',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        className,
      )}
      {...props}
    >
      {children}
    </select>
  );
});

export const Textarea = React.forwardRef(function Textarea({ className, ...props }, ref) {
  return (
    <textarea
      ref={ref}
      className={cn(
        'w-full rounded-md border border-ink-700 bg-ink-950/60 px-3 py-2 text-sm text-slate-100',
        'placeholder:text-slate-500 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent',
        className,
      )}
      {...props}
    />
  );
});

export function Field({ label, hint, error, required, children, className }) {
  return (
    <label className={cn('block space-y-1.5', className)}>
      <span className="flex items-center gap-1 text-xs font-medium uppercase tracking-wide text-slate-400">
        {label}
        {required && <span className="text-dept-police">*</span>}
      </span>
      {children}
      {error ? (
        <span className="block text-xs text-dept-police">{error}</span>
      ) : hint ? (
        <span className="block text-xs text-slate-500">{hint}</span>
      ) : null}
    </label>
  );
}

/* ------------------------------------------------------------------ */
/* Surfaces                                                            */
/* ------------------------------------------------------------------ */

export function Card({ className, children, ...props }) {
  return (
    <div
      className={cn('rounded-lg border border-ink-700 bg-ink-850', className)}
      {...props}
    >
      {children}
    </div>
  );
}

export function CardHeader({ title, description, actions, className }) {
  return (
    <div
      className={cn(
        'flex items-start justify-between gap-4 border-b border-ink-700 px-4 py-3',
        className,
      )}
    >
      <div className="min-w-0">
        <h2 className="truncate text-sm font-semibold text-slate-100">{title}</h2>
        {description && <p className="mt-0.5 text-xs text-slate-400">{description}</p>}
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </div>
  );
}

export function CardBody({ className, children }) {
  return <div className={cn('p-4', className)}>{children}</div>;
}

/* ------------------------------------------------------------------ */
/* Badges and status                                                   */
/* ------------------------------------------------------------------ */

const BADGE_TONES = {
  neutral: 'bg-ink-800 text-slate-300 border-ink-600',
  success: 'bg-state-up/15 text-state-up border-state-up/40',
  danger: 'bg-state-down/15 text-state-down border-state-down/40',
  warn: 'bg-state-warn/15 text-state-warn border-state-warn/40',
  info: 'bg-accent/15 text-accent border-accent/40',
};

export function Badge({ tone = 'neutral', className, children }) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium',
        BADGE_TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}

/** Health/connectivity dot plus label, used by the map popup and the tables. */
export function StatusDot({ state, className }) {
  const tone =
    {
      UP: 'bg-state-up',
      ONLINE: 'bg-state-up',
      ACTIVE: 'bg-state-up',
      DOWN: 'bg-state-down',
      OFFLINE: 'bg-state-down',
      DEGRADED: 'bg-state-warn',
      MAINTENANCE: 'bg-state-warn',
      DUE: 'bg-state-warn',
      OVERDUE: 'bg-state-down',
    }[String(state || '').toUpperCase()] || 'bg-state-unknown';

  return <span className={cn('inline-block h-2 w-2 shrink-0 rounded-full', tone, className)} />;
}

/* ------------------------------------------------------------------ */
/* Feedback                                                            */
/* ------------------------------------------------------------------ */

export function Spinner({ className }) {
  return (
    <span
      role="status"
      aria-label="Loading"
      className={cn(
        'inline-block h-4 w-4 animate-spin rounded-full border-2 border-ink-600 border-t-accent',
        className,
      )}
    />
  );
}

export function EmptyState({ icon: Icon, title, description, action, className }) {
  return (
    <div className={cn('flex flex-col items-center justify-center gap-3 px-6 py-12 text-center', className)}>
      {Icon && <Icon className="h-8 w-8 text-slate-600" aria-hidden />}
      <div>
        <p className="text-sm font-medium text-slate-300">{title}</p>
        {description && <p className="mt-1 max-w-md text-xs text-slate-500">{description}</p>}
      </div>
      {action}
    </div>
  );
}

export function ErrorState({ error, onRetry, className }) {
  return (
    <div className={cn('rounded-md border border-dept-police/40 bg-dept-police/10 p-4', className)}>
      <p className="text-sm font-medium text-dept-police">Something went wrong</p>
      <p className="mt-1 text-xs text-slate-300">
        {error?.detail || error?.message || 'The request failed.'}
      </p>
      {onRetry && (
        <Button size="sm" variant="outline" className="mt-3" onClick={onRetry}>
          Try again
        </Button>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Table                                                               */
/* ------------------------------------------------------------------ */

export function Table({ className, children }) {
  return (
    <div className="w-full overflow-x-auto">
      <table className={cn('w-full border-collapse text-sm', className)}>{children}</table>
    </div>
  );
}

export function Th({ className, children, ...props }) {
  return (
    <th
      scope="col"
      className={cn(
        'sticky top-0 z-10 whitespace-nowrap border-b border-ink-700 bg-ink-850 px-3 py-2 text-left',
        'text-[11px] font-semibold uppercase tracking-wide text-slate-400',
        className,
      )}
      {...props}
    >
      {children}
    </th>
  );
}

export function Td({ className, children, ...props }) {
  return (
    <td className={cn('border-b border-ink-800 px-3 py-2 align-middle text-slate-300', className)} {...props}>
      {children}
    </td>
  );
}
