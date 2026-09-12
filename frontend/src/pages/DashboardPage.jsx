import React from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import {
  Activity,
  AlertTriangle,
  Camera,
  CircleSlash,
  Clock,
  MapPin,
  Wrench,
} from 'lucide-react';

import { PageHeader } from '../components/layout/AppShell.jsx';
import {
  Badge,
  Card,
  CardBody,
  CardHeader,
  ErrorState,
  Spinner,
  StatusDot,
  Table,
  Td,
  Th,
  cn,
} from '../components/ui/index.jsx';
import { api } from '../lib/api.js';
import { useAuth } from '../lib/auth.jsx';

function Kpi({ icon: Icon, label, value, sub, tone = 'neutral', to }) {
  const body = (
    <Card className={cn('h-full transition-colors', to && 'hover:border-ink-600')}>
      <CardBody className="flex items-start gap-3">
        <div
          className={cn(
            'flex h-9 w-9 shrink-0 items-center justify-center rounded-md border',
            tone === 'danger'
              ? 'border-state-down/40 bg-state-down/10 text-state-down'
              : tone === 'warn'
                ? 'border-state-warn/40 bg-state-warn/10 text-state-warn'
                : tone === 'success'
                  ? 'border-state-up/40 bg-state-up/10 text-state-up'
                  : 'border-ink-600 bg-ink-800 text-slate-400',
          )}
        >
          <Icon className="h-4 w-4" aria-hidden />
        </div>
        <div className="min-w-0">
          <p className="text-[11px] uppercase tracking-wide text-slate-500">{label}</p>
          <p className="mt-0.5 text-2xl font-semibold tabular-nums text-slate-100">{value}</p>
          {sub && <p className="mt-0.5 truncate text-xs text-slate-500">{sub}</p>}
        </div>
      </CardBody>
    </Card>
  );
  return to ? <Link to={to}>{body}</Link> : body;
}

export default function DashboardPage() {
  const { user, departmentScope } = useAuth();

  const health = useQuery({
    queryKey: ['health-summary', 24],
    queryFn: () => api.get('/api/v1/reports/health-summary', { window_hours: 24 }),
    // The fleet view is a wall display: keep it current without a reload.
    refetchInterval: 60_000,
  });

  const ageing = useQuery({
    queryKey: ['ageing'],
    queryFn: () => api.get('/api/v1/reports/ageing'),
  });

  const recent = useQuery({
    queryKey: ['cameras', 'recent'],
    queryFn: () =>
      api.get('/api/v1/cameras', { limit: 8, sort_by: 'updated_at', order: 'desc' }),
  });

  const summary = health.data;

  return (
    <div className="min-h-full">
      <PageHeader
        title={`Good ${new Date().getHours() < 12 ? 'morning' : new Date().getHours() < 17 ? 'afternoon' : 'evening'}, ${user?.full_name?.split(' ')[0] || 'operator'}`}
        description={
          departmentScope
            ? `Fleet status for ${departmentScope}. Figures below cover only your department.`
            : 'Statewide fleet status across every integrated department.'
        }
      />

      <div className="space-y-6 p-4 sm:p-6">
        {health.isError && <ErrorState error={health.error} onRetry={health.refetch} />}

        <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
          <Kpi
            icon={Camera}
            label="Cameras"
            value={health.isLoading ? '—' : summary?.total_cameras ?? 0}
            sub="registered and in service"
            to="/registry"
          />
          <Kpi
            icon={Activity}
            label="Online"
            value={health.isLoading ? '—' : summary?.up ?? 0}
            sub={
              summary?.uptime_percent != null
                ? `${summary.uptime_percent}% uptime over 24h`
                : 'no health data in window'
            }
            tone="success"
          />
          <Kpi
            icon={CircleSlash}
            label="Offline"
            value={health.isLoading ? '—' : summary?.down ?? 0}
            sub="last poll unreachable"
            tone={summary?.down ? 'danger' : 'neutral'}
            to="/reports/health"
          />
          <Kpi
            icon={Clock}
            label="Never polled"
            value={health.isLoading ? '—' : summary?.unknown ?? 0}
            sub="status unknown, not down"
          />
          <Kpi
            icon={Wrench}
            label="Maintenance"
            value={health.isLoading ? '—' : summary?.maintenance_attention ?? 0}
            sub="due, overdue or faulty"
            tone={summary?.maintenance_attention ? 'warn' : 'neutral'}
            to="/reports/health"
          />
        </div>

        <div className="grid gap-4 lg:grid-cols-3">
          <Card className="lg:col-span-2">
            <CardHeader
              title="Recently updated cameras"
              description="Latest registry changes, newest first"
              actions={
                <Link to="/registry" className="text-xs text-accent hover:underline">
                  Open registry
                </Link>
              }
            />
            {recent.isLoading ? (
              <div className="flex justify-center p-8">
                <Spinner />
              </div>
            ) : recent.isError ? (
              <CardBody>
                <ErrorState error={recent.error} onRetry={recent.refetch} />
              </CardBody>
            ) : (
              <Table>
                <thead>
                  <tr>
                    <Th>Camera</Th>
                    <Th>Site</Th>
                    <Th>Type</Th>
                    <Th>Status</Th>
                  </tr>
                </thead>
                <tbody>
                  {recent.data?.items?.map((camera) => (
                    <tr key={camera.id} className="hover:bg-ink-800/60">
                      <Td>
                        <Link
                          to={`/registry/${camera.id}`}
                          className="font-mono text-xs text-accent hover:underline"
                        >
                          {camera.global_camera_code}
                        </Link>
                      </Td>
                      <Td className="max-w-[220px] truncate text-xs">
                        {camera.site_name || <span className="text-slate-600">not surveyed</span>}
                      </Td>
                      <Td className="text-xs">{camera.camera_type || '—'}</Td>
                      <Td>
                        <span className="inline-flex items-center gap-1.5 text-xs">
                          <StatusDot state={camera.connectivity_status || camera.status} />
                          {camera.status}
                        </span>
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            )}
          </Card>

          <div className="space-y-4">
            <Card>
              <CardHeader title="Infrastructure age" description="From surveyed installation dates" />
              <CardBody className="space-y-2">
                {ageing.isLoading ? (
                  <Spinner />
                ) : (
                  <>
                    {ageing.data?.bands?.map((band) => (
                      <div key={band.band} className="flex items-center justify-between text-sm">
                        <span className="text-slate-400">{band.band}</span>
                        <span className="tabular-nums text-slate-200">{band.camera_count}</span>
                      </div>
                    ))}
                    {ageing.data?.unknown_installation_date > 0 && (
                      <p className="mt-3 rounded border border-state-warn/30 bg-state-warn/10 px-2 py-1.5 text-[11px] text-state-warn">
                        {ageing.data.unknown_installation_date} camera(s) have no installation date
                        recorded, so this report understates the ageing fleet.
                      </p>
                    )}
                  </>
                )}
              </CardBody>
            </Card>

            <Card>
              <CardHeader title="Jump to" />
              <CardBody className="grid grid-cols-2 gap-2 text-xs">
                <Link
                  to="/map"
                  className="flex items-center gap-2 rounded-md border border-ink-700 px-3 py-2 hover:bg-ink-800"
                >
                  <MapPin className="h-3.5 w-3.5 text-accent" /> GIS map
                </Link>
                <Link
                  to="/alerts"
                  className="flex items-center gap-2 rounded-md border border-ink-700 px-3 py-2 hover:bg-ink-800"
                >
                  <AlertTriangle className="h-3.5 w-3.5 text-dept-police" /> Alerts
                </Link>
                <Link
                  to="/reports/gap-analysis"
                  className="flex items-center gap-2 rounded-md border border-ink-700 px-3 py-2 hover:bg-ink-800"
                >
                  <Camera className="h-3.5 w-3.5 text-slate-400" /> Coverage gaps
                </Link>
                <Link
                  to="/wall"
                  className="flex items-center gap-2 rounded-md border border-ink-700 px-3 py-2 hover:bg-ink-800"
                >
                  <Activity className="h-3.5 w-3.5 text-slate-400" /> Video wall
                </Link>
              </CardBody>
            </Card>
          </div>
        </div>

        {summary?.maintenance_due?.length > 0 && (
          <Card>
            <CardHeader
              title="Maintenance attention"
              description="Cameras flagged due, overdue or faulty"
              actions={<Badge tone="warn">{summary.maintenance_due.length}</Badge>}
            />
            <Table>
              <thead>
                <tr>
                  <Th>Camera</Th>
                  <Th>Site</Th>
                  <Th>State</Th>
                  <Th>Due</Th>
                  <Th>Work order</Th>
                </tr>
              </thead>
              <tbody>
                {summary.maintenance_due.slice(0, 10).map((row) => (
                  <tr key={row.camera_id} className="hover:bg-ink-800/60">
                    <Td>
                      <Link
                        to={`/registry/${row.camera_id}`}
                        className="font-mono text-xs text-accent hover:underline"
                      >
                        {row.global_camera_code}
                      </Link>
                    </Td>
                    <Td className="text-xs">{row.site_name || '—'}</Td>
                    <Td>
                      <Badge tone={row.maintenance_state === 'OVERDUE' ? 'danger' : 'warn'}>
                        {row.maintenance_state}
                      </Badge>
                    </Td>
                    <Td className="text-xs">{row.next_service_due || '—'}</Td>
                    <Td className="text-xs">{row.work_order_ref || '—'}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </Card>
        )}
      </div>
    </div>
  );
}
