/**
 * Console chrome: sidebar, top bar, and the routed content area.
 *
 * Navigation is filtered by role at render time, but that is a convenience -
 * the server refuses the request regardless. Hiding a link the user cannot use
 * keeps the console honest about what this operator can actually do.
 */

import React, { useState } from 'react';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import {
  Activity,
  AlertTriangle,
  Camera,
  ClipboardList,
  Grid2X2,
  LayoutDashboard,
  LogOut,
  Map as MapIcon,
  Menu,
  Network,
  PlayCircle,
  Search,
  ShieldCheck,
  Users,
} from 'lucide-react';

import { Badge, Button, cn } from '../ui/index.jsx';
import { ROLE_LABELS, useAuth } from '../../lib/auth.jsx';

// `model` is shown in the sidebar so an evaluator can see at a glance which
// tender model each screen belongs to.
const NAV_SECTIONS = [
  {
    label: 'Overview',
    items: [
      { to: '/', end: true, icon: LayoutDashboard, label: 'Dashboard', model: null },
      { to: '/map', icon: MapIcon, label: 'GIS Map', model: 'M1' },
    ],
  },
  {
    label: 'Registry',
    items: [
      { to: '/registry', icon: Camera, label: 'Camera Registry', model: 'M1' },
      { to: '/reports/gap-analysis', icon: ClipboardList, label: 'Gap Analysis', model: 'M1' },
      { to: '/reports/health', icon: Activity, label: 'Health & Maintenance', model: 'M1' },
    ],
  },
  {
    label: 'Operations',
    items: [
      { to: '/wall', icon: Grid2X2, label: 'Video Wall', model: 'M2' },
      { to: '/search/vehicles', icon: Search, label: 'Vehicle Search', model: 'M2' },
      { to: '/alerts', icon: AlertTriangle, label: 'Alerts', model: 'M2' },
    ],
  },
  {
    label: 'Integration',
    items: [
      { to: '/federation', icon: Network, label: 'VMS Federation', model: 'M3' },
      { to: '/vms/playback', icon: PlayCircle, label: 'Recording & Playback', model: 'M4' },
    ],
  },
  {
    label: 'Administration',
    minimumRole: 'DEPT_ADMIN',
    items: [
      { to: '/admin/users', icon: Users, label: 'Users & Roles', model: 'M1' },
      { to: '/admin/audit', icon: ShieldCheck, label: 'Audit Trail', model: 'M1' },
    ],
  },
];

function NavItem({ item, onNavigate }) {
  return (
    <NavLink
      to={item.to}
      end={item.end}
      onClick={onNavigate}
      className={({ isActive }) =>
        cn(
          'group flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors',
          isActive
            ? 'bg-accent/15 text-slate-100 ring-1 ring-inset ring-accent/40'
            : 'text-slate-400 hover:bg-ink-800 hover:text-slate-200',
        )
      }
    >
      <item.icon className="h-4 w-4 shrink-0" aria-hidden />
      <span className="flex-1 truncate">{item.label}</span>
      {item.model && (
        <span className="shrink-0 rounded bg-ink-800 px-1.5 py-0.5 font-mono text-[10px] text-slate-500 group-hover:text-slate-400">
          {item.model}
        </span>
      )}
    </NavLink>
  );
}

export default function AppShell() {
  const { user, signOut, atLeast } = useAuth();
  const [mobileOpen, setMobileOpen] = useState(false);
  const location = useLocation();

  const closeMobile = () => setMobileOpen(false);

  const sidebar = (
    <nav className="flex h-full flex-col gap-6 overflow-y-auto px-3 py-4">
      {NAV_SECTIONS.filter(
        (section) => !section.minimumRole || atLeast(section.minimumRole),
      ).map((section) => (
        <div key={section.label}>
          <p className="px-3 pb-2 text-[10px] font-semibold uppercase tracking-widest text-slate-600">
            {section.label}
          </p>
          <div className="space-y-0.5">
            {section.items.map((item) => (
              <NavItem key={item.to} item={item} onNavigate={closeMobile} />
            ))}
          </div>
        </div>
      ))}
    </nav>
  );

  return (
    <div className="flex h-full w-full flex-col bg-ink-900">
      <header className="flex h-14 shrink-0 items-center gap-3 border-b border-ink-700 bg-ink-850 px-3 sm:px-4">
        <Button
          variant="ghost"
          size="icon"
          className="lg:hidden"
          aria-label="Toggle navigation"
          onClick={() => setMobileOpen((open) => !open)}
        >
          <Menu className="h-5 w-5" />
        </Button>

        <div className="flex min-w-0 items-baseline gap-3">
          <span className="text-lg font-bold tracking-[0.18em] text-slate-100">TRINETRA</span>
          <span className="hidden truncate text-xs text-slate-500 sm:block">
            Gujarat CCTV Integration Platform
          </span>
        </div>

        <div className="ml-auto flex items-center gap-3">
          {user && (
            <>
              <div className="hidden text-right sm:block">
                <p className="text-xs font-medium text-slate-200">{user.full_name}</p>
                <p className="text-[11px] text-slate-500">
                  {ROLE_LABELS[user.role] || user.role}
                  {user.department_id ? ` · ${user.department_id}` : ' · All departments'}
                </p>
              </div>
              <Badge tone={user.role === 'SUPER_ADMIN' ? 'info' : 'neutral'}>
                {user.department_id || 'STATE'}
              </Badge>
            </>
          )}
          <Button variant="ghost" size="icon" aria-label="Sign out" onClick={signOut}>
            <LogOut className="h-4 w-4" />
          </Button>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        <aside className="hidden w-60 shrink-0 border-r border-ink-700 bg-ink-850 lg:block">
          {sidebar}
        </aside>

        {/* Mobile drawer. Rendered only when open so it cannot trap focus while
            invisible. */}
        {mobileOpen && (
          <div className="fixed inset-0 z-40 lg:hidden">
            <div
              className="absolute inset-0 bg-black/60"
              onClick={closeMobile}
              aria-hidden
            />
            <aside className="absolute left-0 top-14 bottom-0 w-64 border-r border-ink-700 bg-ink-850">
              {sidebar}
            </aside>
          </div>
        )}

        <main key={location.pathname} className="min-w-0 flex-1 overflow-y-auto">
          <Outlet />
        </main>
      </div>
    </div>
  );
}

/** Standard page heading used by every routed screen. */
export function PageHeader({ title, description, actions, children }) {
  return (
    <div className="border-b border-ink-700 bg-ink-850/60 px-4 py-4 sm:px-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold text-slate-100">{title}</h1>
          {description && <p className="mt-1 text-sm text-slate-400">{description}</p>}
        </div>
        {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
      </div>
      {children}
    </div>
  );
}
