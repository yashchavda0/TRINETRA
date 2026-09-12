import React, { useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Cell, Legend, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts';

import { PageHeader } from '../components/layout/AppShell.jsx';
import {
  Badge,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorState,
  Field,
  Select,
  Spinner,
  Table,
  Td,
  Th,
} from '../components/ui/index.jsx';
import { api } from '../lib/api.js';

const WINDOWS = [
  { value: 1, label: 'Last hour' },
  { value: 24, label: 'Last 24 hours' },
  { value: 168, label: 'Last 7 days' },
  { value: 720, label: 'Last 30 days' },
];

export default function HealthPage() {
  const [window, setWindow] = useState(24);

  const health = useQuery({
    queryKey: ['health-summary', window],
    queryFn: () => api.get('/api/v1/reports/health-summary', { window_hours: window }),
    refetchInterval: 30_000,
  });

  const offline = useQuery({
    queryKey: ['cameras', 'offline'],
    queryFn: () => api.get('/api/v1/cameras', { connectivity: 'OFFLINE', limit: 100 }),
  });

  const data = health.data;
  const pie = data
    ? [
        { name: 'Online', value: data.up, fill: '#17a94b' },
        { name: 'Offline', value: data.down, fill: '#e02020' },
        { name: 'Never polled', value: data.unknown, fill: '#6b7280' },
      ].filter((slice) => slice.value > 0)
    : [];

  return (
    <div className="min-h-full">
      <PageHeader
        title="Health & maintenance"
        description="Reachability telemetry and service condition across the fleet"
        actions={
          <Field label="Window" className="w-40">
            <Select value={window} onChange={(event) => setWindow(Number(event.target.value))}>
              {WINDOWS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </Select>
          </Field>
        }
      />

      <div className="space-y-4 p-4 sm:p-6">
        {health.isError && <ErrorState error={health.error} onRetry={health.refetch} />}

        <div className="grid gap-4 lg:grid-cols-3">
          <Card className="lg:col-span-1">
            <CardHeader title="Reachability" description="Latest poll per camera" />
            <CardBody>
              {health.isLoading ? (
                <Spinner />
              ) : pie.length === 0 ? (
                <p className="text-xs text-slate-500">No cameras in scope.</p>
              ) : (
                <div className="h-56">
                  <ResponsiveContainer width="100%" height="100%">
                    <PieChart>
                      <Pie
                        data={pie}
                        dataKey="value"
                        nameKey="name"
                        innerRadius={50}
                        outerRadius={80}
                        paddingAngle={2}
                      >
                        {pie.map((slice) => (
                          <Cell key={slice.name} fill={slice.fill} />
                        ))}
                      </Pie>
                      <Legend wrapperStyle={{ fontSize: 11, color: '#94a3b8' }} />
                      <Tooltip
                        contentStyle={{
                          background: '#0f1620',
                          border: '1px solid #1f2a37',
                          borderRadius: 6,
                          fontSize: 12,
                        }}
                      />
                    </PieChart>
                  </ResponsiveContainer>
                </div>
              )}
            </CardBody>
          </Card>

          <Card className="lg:col-span-2">
            <CardHeader title="Availability" description={`Computed over the ${window}h window`} />
            <CardBody className="grid grid-cols-2 gap-4 sm:grid-cols-4">
              <div>
                <p className="text-[11px] uppercase tracking-wide text-slate-500">Uptime</p>
                <p className="mt-1 text-2xl font-semibold tabular-nums text-slate-100">
                  {data?.uptime_percent != null ? `${data.uptime_percent}%` : '—'}
                </p>
                {data?.uptime_percent == null && (
                  <p className="mt-1 text-[11px] text-slate-500">
                    no polls landed in this window
                  </p>
                )}
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-slate-500">Polls</p>
                <p className="mt-1 text-2xl font-semibold tabular-nums text-slate-100">
                  {data?.pings_in_window ?? '—'}
                </p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-slate-500">Offline now</p>
                <p className="mt-1 text-2xl font-semibold tabular-nums text-state-down">
                  {data?.down ?? '—'}
                </p>
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-wide text-slate-500">Never polled</p>
                <p className="mt-1 text-2xl font-semibold tabular-nums text-slate-400">
                  {data?.unknown ?? '—'}
                </p>
              </div>
            </CardBody>
          </Card>
        </div>

        <Card>
          <CardHeader
            title="Maintenance attention"
            description="Due, overdue, faulty, or past their service date"
            actions={
              data?.maintenance_due?.length ? (
                <Badge tone="warn">{data.maintenance_due.length}</Badge>
              ) : null
            }
          />
          {health.isLoading ? (
            <CardBody>
              <Spinner />
            </CardBody>
          ) : data?.maintenance_due?.length ? (
            <Table>
              <thead>
                <tr>
                  <Th>Camera</Th>
                  <Th>Site</Th>
                  <Th>State</Th>
                  <Th>Service due</Th>
                  <Th>Work order</Th>
                </tr>
              </thead>
              <tbody>
                {data.maintenance_due.map((row) => (
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
                    <Td className="text-xs tabular-nums">{row.next_service_due || '—'}</Td>
                    <Td className="text-xs">{row.work_order_ref || '—'}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <EmptyState
              title="Nothing needs attention"
              description="No camera is flagged due, overdue or faulty in your scope."
            />
          )}
        </Card>

        <Card>
          <CardHeader
            title="Currently offline"
            description="Cameras whose connectivity status is OFFLINE"
          />
          {offline.isLoading ? (
            <CardBody>
              <Spinner />
            </CardBody>
          ) : offline.data?.items?.length ? (
            <Table>
              <thead>
                <tr>
                  <Th>Camera</Th>
                  <Th>Site</Th>
                  <Th>Department</Th>
                  <Th>Link</Th>
                  <Th>Last seen</Th>
                </tr>
              </thead>
              <tbody>
                {offline.data.items.map((camera) => (
                  <tr key={camera.id} className="hover:bg-ink-800/60">
                    <Td>
                      <Link
                        to={`/registry/${camera.id}`}
                        className="font-mono text-xs text-accent hover:underline"
                      >
                        {camera.global_camera_code}
                      </Link>
                    </Td>
                    <Td className="text-xs">{camera.site_name || '—'}</Td>
                    <Td className="text-xs">{camera.department_id}</Td>
                    <Td className="text-xs">{camera.connectivity_type || '—'}</Td>
                    <Td className="text-xs">
                      {camera.last_seen_at
                        ? new Date(camera.last_seen_at).toLocaleString()
                        : 'never'}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <EmptyState
              title="No cameras marked offline"
              description="Connectivity status is set by the health poller; cameras never polled read as UNKNOWN rather than offline."
            />
          )}
        </Card>
      </div>
    </div>
  );
}
