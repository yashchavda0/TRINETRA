import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { AlertCircle, Info, MapPinned } from 'lucide-react';

import { PageHeader } from '../components/layout/AppShell.jsx';
import {
  Badge,
  Button,
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

const RANGES = [60, 90, 120, 150, 200, 300];

function coverageTone(percent) {
  if (percent >= 60) return '#17a94b';
  if (percent >= 30) return '#f0a020';
  return '#e02020';
}

export default function GapAnalysisPage() {
  const [range, setRange] = useState(120);
  const [level, setLevel] = useState('WARD');

  const coverage = useQuery({
    queryKey: ['coverage', range],
    queryFn: () =>
      api.get('/api/v1/reports/coverage', { range_meters: range, include_geometry: false }),
  });

  const density = useQuery({
    queryKey: ['density', level],
    queryFn: () => api.get('/api/v1/reports/density', { level }),
  });

  const ageing = useQuery({
    queryKey: ['ageing', 7],
    queryFn: () => api.get('/api/v1/reports/ageing', { replacement_years: 7 }),
  });

  const boundaries = coverage.data?.boundaries ?? [];
  const noBoundaries = !coverage.isLoading && boundaries.length === 0;

  return (
    <div className="min-h-full">
      <PageHeader
        title="Gap analysis"
        description="Where the fleet is blind, thin, or ageing out of service"
        actions={
          <Field label="Camera range" className="w-36">
            <Select value={range} onChange={(event) => setRange(Number(event.target.value))}>
              {RANGES.map((value) => (
                <option key={value} value={value}>
                  {value} m
                </option>
              ))}
            </Select>
          </Field>
        }
      />

      <div className="space-y-4 p-4 sm:p-6">
        {coverage.isError && <ErrorState error={coverage.error} onRetry={coverage.refetch} />}

        <div className="grid gap-4 sm:grid-cols-3">
          <Card>
            <CardBody>
              <p className="text-[11px] uppercase tracking-wide text-slate-500">Cameras analysed</p>
              <p className="mt-1 text-2xl font-semibold tabular-nums text-slate-100">
                {coverage.isLoading ? '—' : coverage.data.camera_count}
              </p>
              <p className="mt-1 text-xs text-slate-500">active cameras with a known position</p>
            </CardBody>
          </Card>
          <Card>
            <CardBody>
              <p className="text-[11px] uppercase tracking-wide text-slate-500">Nominal coverage</p>
              <p className="mt-1 text-2xl font-semibold tabular-nums text-slate-100">
                {coverage.isLoading ? '—' : `${coverage.data.covered_sq_km} km²`}
              </p>
              <p className="mt-1 text-xs text-slate-500">union of all viewsheds at {range} m</p>
            </CardBody>
          </Card>
          <Card>
            <CardBody>
              <p className="text-[11px] uppercase tracking-wide text-slate-500">
                Due for replacement
              </p>
              <p className="mt-1 text-2xl font-semibold tabular-nums text-slate-100">
                {ageing.isLoading ? '—' : ageing.data.due_for_replacement}
              </p>
              <p className="mt-1 text-xs text-slate-500">installed over 7 years ago</p>
            </CardBody>
          </Card>
        </div>

        <div className="flex items-start gap-2 rounded-md border border-accent/30 bg-accent/10 px-3 py-2 text-xs text-slate-300">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" aria-hidden />
          <p>{coverage.data?.caveat || 'Coverage is a nominal sector, not a line-of-sight model.'}</p>
        </div>

        <Card>
          <CardHeader
            title="Coverage by administrative area"
            description="Worst covered first — this is the work list"
            actions={
              <Field label="Level" className="w-32">
                <Select value={level} onChange={(event) => setLevel(event.target.value)}>
                  {['WARD', 'ZONE', 'CITY', 'DISTRICT'].map((option) => (
                    <option key={option}>{option}</option>
                  ))}
                </Select>
              </Field>
            }
          />
          {coverage.isLoading ? (
            <CardBody>
              <Spinner />
            </CardBody>
          ) : noBoundaries ? (
            <EmptyState
              icon={MapPinned}
              title="No administrative boundaries loaded"
              description={
                'Coverage per ward, zone or district needs boundary polygons in the ' +
                'admin_boundaries table. Load your ward/zone shapefiles to turn this into ' +
                'a per-jurisdiction report; until then only the fleet-wide totals above are ' +
                'meaningful.'
              }
            />
          ) : (
            <>
              <div className="h-64 px-2 py-4">
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={boundaries.slice(0, 15)}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#1f2a37" />
                    <XAxis dataKey="name" tick={{ fill: '#6b7280', fontSize: 11 }} />
                    <YAxis
                      tick={{ fill: '#6b7280', fontSize: 11 }}
                      unit="%"
                      domain={[0, 100]}
                    />
                    <Tooltip
                      contentStyle={{
                        background: '#0f1620',
                        border: '1px solid #1f2a37',
                        borderRadius: 6,
                        fontSize: 12,
                      }}
                    />
                    <Bar dataKey="coverage_percent" name="Coverage %">
                      {boundaries.slice(0, 15).map((row) => (
                        <Cell key={row.boundary_id} fill={coverageTone(row.coverage_percent)} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>

              <Table>
                <thead>
                  <tr>
                    <Th>Area</Th>
                    <Th>Level</Th>
                    <Th className="text-right">Area km²</Th>
                    <Th className="text-right">Covered km²</Th>
                    <Th className="text-right">Coverage</Th>
                    <Th className="text-right">Cameras</Th>
                  </tr>
                </thead>
                <tbody>
                  {boundaries.map((row) => (
                    <tr key={row.boundary_id} className="hover:bg-ink-800/60">
                      <Td className="text-xs">{row.name}</Td>
                      <Td className="text-xs text-slate-500">{row.level}</Td>
                      <Td className="text-right text-xs tabular-nums">{row.area_sq_km}</Td>
                      <Td className="text-right text-xs tabular-nums">{row.covered_sq_km}</Td>
                      <Td className="text-right">
                        <Badge
                          tone={
                            row.coverage_percent >= 60
                              ? 'success'
                              : row.coverage_percent >= 30
                                ? 'warn'
                                : 'danger'
                          }
                        >
                          {row.coverage_percent}%
                        </Badge>
                      </Td>
                      <Td className="text-right text-xs tabular-nums">{row.camera_count}</Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </>
          )}
        </Card>

        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader title="Camera density" description={`Cameras per km² by ${level.toLowerCase()}`} />
            {density.isLoading ? (
              <CardBody>
                <Spinner />
              </CardBody>
            ) : density.data?.length ? (
              <Table>
                <thead>
                  <tr>
                    <Th>Area</Th>
                    <Th className="text-right">Cameras</Th>
                    <Th className="text-right">Per km²</Th>
                  </tr>
                </thead>
                <tbody>
                  {density.data.map((row) => (
                    <tr key={row.boundary_id}>
                      <Td className="text-xs">{row.name}</Td>
                      <Td className="text-right text-xs tabular-nums">{row.camera_count}</Td>
                      <Td className="text-right text-xs tabular-nums">{row.cameras_per_sq_km}</Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            ) : (
              <CardBody>
                <p className="text-xs text-slate-500">
                  No boundaries at this level. Density is computed per polygon, so it needs the
                  same boundary data as the coverage report.
                </p>
              </CardBody>
            )}
          </Card>

          <Card>
            <CardHeader
              title="Ageing infrastructure"
              description="By surveyed installation date"
            />
            <CardBody className="space-y-3">
              {ageing.isLoading ? (
                <Spinner />
              ) : (
                <>
                  <div className="h-48">
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart data={ageing.data.bands}>
                        <CartesianGrid strokeDasharray="3 3" stroke="#1f2a37" />
                        <XAxis dataKey="band" tick={{ fill: '#6b7280', fontSize: 11 }} />
                        <YAxis tick={{ fill: '#6b7280', fontSize: 11 }} allowDecimals={false} />
                        <Tooltip
                          contentStyle={{
                            background: '#0f1620',
                            border: '1px solid #1f2a37',
                            borderRadius: 6,
                            fontSize: 12,
                          }}
                        />
                        <Bar dataKey="camera_count" name="Cameras" fill="#2f81f7" />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>

                  {ageing.data.unknown_installation_date > 0 && (
                    <div className="flex items-start gap-2 rounded-md border border-state-warn/40 bg-state-warn/10 px-3 py-2 text-xs text-state-warn">
                      <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                      <p>
                        {ageing.data.unknown_installation_date} of the fleet has no installation
                        date recorded. Those cameras cannot be aged, so the replacement figure is
                        a floor, not a total. Capturing installation dates during survey closes
                        this gap.
                      </p>
                    </div>
                  )}
                </>
              )}
            </CardBody>
          </Card>
        </div>
      </div>
    </div>
  );
}
