import React, { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ArrowDown,
  ArrowUp,
  Camera,
  Download,
  FileUp,
  Filter,
  Search,
  X,
} from 'lucide-react';

import { PageHeader } from '../components/layout/AppShell.jsx';
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Field,
  Input,
  Select,
  Spinner,
  StatusDot,
  Table,
  Td,
  Th,
  cn,
} from '../components/ui/index.jsx';
import { api, request, tokenStore } from '../lib/api.js';
import { useAuth } from '../lib/auth.jsx';

const DEPARTMENTS = ['POLICE', 'RTO', 'GSRTC', 'CIVIL_SUPPLIES', 'REVENUE', 'PRIVATE'];
const CAMERA_TYPES = ['FIXED', 'PTZ', 'DOME', 'BULLET', 'ANPR', 'THERMAL', 'PANORAMIC', 'OTHER'];
const STATUSES = ['ACTIVE', 'INACTIVE', 'MAINTENANCE', 'DECOMMISSIONED'];
const PAGE_SIZE = 25;

const SORT_COLUMNS = [
  { key: 'global_camera_code', label: 'Camera code' },
  { key: 'site_name', label: 'Site' },
  { key: 'department_id', label: 'Department' },
  { key: 'camera_type', label: 'Type' },
  { key: 'status', label: 'Status' },
  { key: 'installed_on', label: 'Installed' },
  { key: 'updated_at', label: 'Updated' },
];

/* ------------------------------------------------------------------ */
/* Bulk import                                                         */
/* ------------------------------------------------------------------ */

function BulkImportPanel({ onClose }) {
  const queryClient = useQueryClient();
  const [file, setFile] = useState(null);
  const [updateExisting, setUpdateExisting] = useState(false);
  const [result, setResult] = useState(null);

  const upload = useMutation({
    mutationFn: async ({ commit }) => {
      const form = new FormData();
      form.append('file', file);
      // The default is a dry run on purpose: an operator should see the
      // rejected rows before anything is written.
      return request(
        `/api/v1/cameras/bulk?dry_run=${commit ? 'false' : 'true'}&update_existing=${updateExisting}`,
        { method: 'POST', body: form },
      );
    },
    onSuccess: (data) => {
      setResult(data);
      if (!data.dry_run) queryClient.invalidateQueries({ queryKey: ['cameras'] });
    },
  });

  return (
    <Card className="border-accent/40">
      <div className="flex items-start justify-between border-b border-ink-700 px-4 py-3">
        <div>
          <h2 className="text-sm font-semibold text-slate-100">Bulk onboarding</h2>
          <p className="mt-0.5 text-xs text-slate-400">
            Upload a CSV or Excel file. Validation runs first and writes nothing.
          </p>
        </div>
        <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close import panel">
          <X className="h-4 w-4" />
        </Button>
      </div>

      <div className="space-y-4 p-4">
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Import file" className="min-w-[260px] flex-1">
            <input
              type="file"
              accept=".csv,.xlsx,.xlsm"
              onChange={(event) => {
                setFile(event.target.files?.[0] || null);
                setResult(null);
              }}
              className="block w-full text-xs text-slate-400 file:mr-3 file:rounded-md file:border file:border-ink-600 file:bg-ink-800 file:px-3 file:py-1.5 file:text-xs file:text-slate-200 hover:file:bg-ink-700"
            />
          </Field>

          <label className="mb-1.5 flex items-center gap-2 text-xs text-slate-400">
            <input
              type="checkbox"
              checked={updateExisting}
              onChange={(event) => setUpdateExisting(event.target.checked)}
              className="h-3.5 w-3.5 rounded border-ink-600 bg-ink-900"
            />
            Overwrite existing camera codes
          </label>

          <Button
            variant="secondary"
            disabled={!file || upload.isPending}
            onClick={() => upload.mutate({ commit: false })}
          >
            {upload.isPending ? <Spinner className="h-3.5 w-3.5" /> : 'Validate'}
          </Button>
          <Button
            variant="primary"
            disabled={!file || upload.isPending || !result || result.valid_rows === 0}
            onClick={() => upload.mutate({ commit: true })}
          >
            Import {result ? `${result.valid_rows} row(s)` : ''}
          </Button>
          <a
            href="/api/v1/cameras/import-template.csv"
            onClick={async (event) => {
              // The template endpoint needs the bearer token, which a plain
              // <a href> cannot carry - fetch it and hand the browser a blob.
              event.preventDefault();
              const response = await request('/api/v1/cameras/import-template.csv', { raw: true });
              const blob = await response.blob();
              const url = URL.createObjectURL(blob);
              const link = document.createElement('a');
              link.href = url;
              link.download = 'trinetra-camera-import-template.csv';
              link.click();
              URL.revokeObjectURL(url);
            }}
            className="mb-1.5 text-xs text-accent hover:underline"
          >
            Download template
          </a>
        </div>

        {upload.isError && <ErrorState error={upload.error} />}

        {result && (
          <div
            className={cn(
              'rounded-md border px-3 py-2 text-xs',
              result.failed_rows?.length
                ? 'border-state-warn/40 bg-state-warn/10 text-state-warn'
                : 'border-state-up/40 bg-state-up/10 text-state-up',
            )}
          >
            {result.message}
          </div>
        )}

        {result?.failed_rows?.length > 0 && (
          <div className="max-h-64 overflow-y-auto rounded-md border border-ink-700">
            <Table>
              <thead>
                <tr>
                  <Th className="w-16">Row</Th>
                  <Th className="w-40">Camera code</Th>
                  <Th>Why it was rejected</Th>
                </tr>
              </thead>
              <tbody>
                {result.failed_rows.map((row, index) => (
                  <tr key={`${row.row_number}-${index}`}>
                    <Td className="tabular-nums text-xs">
                      {row.row_number || <span className="text-slate-600">—</span>}
                    </Td>
                    <Td className="font-mono text-xs">{row.global_camera_code || '—'}</Td>
                    <Td className="text-xs text-state-warn">{row.errors.join('; ')}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </div>
        )}
      </div>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/* Registry                                                            */
/* ------------------------------------------------------------------ */

export default function RegistryPage() {
  const { atLeast, departmentScope } = useAuth();
  const [search, setSearch] = useState('');
  const [debounced, setDebounced] = useState('');
  const [filters, setFilters] = useState({
    department_id: '',
    status: '',
    camera_type: '',
    connectivity: '',
    maintenance: '',
  });
  const [sort, setSort] = useState({ by: 'global_camera_code', order: 'asc' });
  const [page, setPage] = useState(0);
  const [showImport, setShowImport] = useState(false);
  const [showFilters, setShowFilters] = useState(false);

  // Debounce so a fast typist does not fire a query per keystroke.
  React.useEffect(() => {
    const timer = setTimeout(() => {
      setDebounced(search.trim());
      setPage(0);
    }, 300);
    return () => clearTimeout(timer);
  }, [search]);

  const params = useMemo(
    () => ({
      q: debounced || undefined,
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
      sort_by: sort.by,
      order: sort.order,
      ...Object.fromEntries(Object.entries(filters).filter(([, value]) => value)),
    }),
    [debounced, page, sort, filters],
  );

  const cameras = useQuery({
    queryKey: ['cameras', params],
    queryFn: () => api.get('/api/v1/cameras', params),
    placeholderData: (previous) => previous,
  });

  const total = cameras.data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const activeFilters = Object.values(filters).filter(Boolean).length;

  function toggleSort(key) {
    setSort((current) =>
      current.by === key
        ? { by: key, order: current.order === 'asc' ? 'desc' : 'asc' }
        : { by: key, order: 'asc' },
    );
  }

  async function exportCsv() {
    const response = await request('/api/v1/cameras/export.csv', { raw: true });
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `trinetra-cameras-${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  }

  return (
    <div className="min-h-full">
      <PageHeader
        title="Camera registry"
        description={
          departmentScope
            ? `Cameras owned by ${departmentScope}.`
            : 'Every camera onboarded across all departments.'
        }
        actions={
          <>
            <Button variant="secondary" size="sm" onClick={exportCsv}>
              <Download className="h-3.5 w-3.5" /> Export CSV
            </Button>
            {atLeast('DEPT_ADMIN') && (
              <Button
                variant="primary"
                size="sm"
                onClick={() => setShowImport((open) => !open)}
              >
                <FileUp className="h-3.5 w-3.5" /> Bulk import
              </Button>
            )}
          </>
        }
      />

      <div className="space-y-4 p-4 sm:p-6">
        {showImport && <BulkImportPanel onClose={() => setShowImport(false)} />}

        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-[220px] flex-1">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-500" />
            <Input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search code, site, make, model, ward, owner…"
              className="pl-8"
            />
          </div>
          <Button
            variant={showFilters || activeFilters ? 'primary' : 'secondary'}
            size="md"
            onClick={() => setShowFilters((open) => !open)}
          >
            <Filter className="h-3.5 w-3.5" />
            Filters {activeFilters > 0 && `(${activeFilters})`}
          </Button>
          <span className="text-xs tabular-nums text-slate-500">
            {cameras.isFetching ? 'loading…' : `${total} camera${total === 1 ? '' : 's'}`}
          </span>
        </div>

        {showFilters && (
          <Card>
            <div className="grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-5">
              {!departmentScope && (
                <Field label="Department">
                  <Select
                    value={filters.department_id}
                    onChange={(e) => {
                      setFilters((f) => ({ ...f, department_id: e.target.value }));
                      setPage(0);
                    }}
                  >
                    <option value="">All</option>
                    {DEPARTMENTS.map((d) => (
                      <option key={d} value={d}>
                        {d}
                      </option>
                    ))}
                  </Select>
                </Field>
              )}
              <Field label="Lifecycle status">
                <Select
                  value={filters.status}
                  onChange={(e) => {
                    setFilters((f) => ({ ...f, status: e.target.value }));
                    setPage(0);
                  }}
                >
                  <option value="">All</option>
                  {STATUSES.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Camera type">
                <Select
                  value={filters.camera_type}
                  onChange={(e) => {
                    setFilters((f) => ({ ...f, camera_type: e.target.value }));
                    setPage(0);
                  }}
                >
                  <option value="">All</option>
                  {CAMERA_TYPES.map((t) => (
                    <option key={t} value={t}>
                      {t}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Connectivity">
                <Select
                  value={filters.connectivity}
                  onChange={(e) => {
                    setFilters((f) => ({ ...f, connectivity: e.target.value }));
                    setPage(0);
                  }}
                >
                  <option value="">All</option>
                  {['ONLINE', 'OFFLINE', 'DEGRADED', 'UNKNOWN'].map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Maintenance">
                <Select
                  value={filters.maintenance}
                  onChange={(e) => {
                    setFilters((f) => ({ ...f, maintenance: e.target.value }));
                    setPage(0);
                  }}
                >
                  <option value="">All</option>
                  {['OK', 'DUE', 'OVERDUE', 'IN_PROGRESS', 'FAULTY'].map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
          </Card>
        )}

        <Card className="overflow-hidden">
          {cameras.isLoading ? (
            <div className="flex justify-center p-12">
              <Spinner className="h-6 w-6" />
            </div>
          ) : cameras.isError ? (
            <div className="p-4">
              <ErrorState error={cameras.error} onRetry={cameras.refetch} />
            </div>
          ) : total === 0 ? (
            <EmptyState
              icon={Camera}
              title="No cameras match"
              description={
                debounced || activeFilters
                  ? 'Try a different search term or clear the filters.'
                  : 'Onboard cameras manually or with a bulk import to populate the registry.'
              }
            />
          ) : (
            <div className="max-h-[calc(100vh-19rem)] overflow-auto">
              <Table>
                <thead>
                  <tr>
                    {SORT_COLUMNS.map((column) => (
                      <Th key={column.key}>
                        <button
                          type="button"
                          onClick={() => toggleSort(column.key)}
                          className="inline-flex items-center gap-1 hover:text-slate-200"
                        >
                          {column.label}
                          {sort.by === column.key &&
                            (sort.order === 'asc' ? (
                              <ArrowUp className="h-3 w-3" />
                            ) : (
                              <ArrowDown className="h-3 w-3" />
                            ))}
                        </button>
                      </Th>
                    ))}
                    <Th>Health</Th>
                  </tr>
                </thead>
                <tbody>
                  {cameras.data.items.map((camera) => (
                    <tr key={camera.id} className="hover:bg-ink-800/60">
                      <Td>
                        <Link
                          to={`/registry/${camera.id}`}
                          className="font-mono text-xs text-accent hover:underline"
                        >
                          {camera.global_camera_code}
                        </Link>
                      </Td>
                      <Td className="max-w-[240px] truncate text-xs">
                        {camera.site_name || <span className="text-slate-600">not surveyed</span>}
                      </Td>
                      <Td className="text-xs">{camera.department_id}</Td>
                      <Td className="text-xs">
                        {camera.camera_type || <span className="text-slate-600">—</span>}
                      </Td>
                      <Td>
                        <Badge
                          tone={
                            camera.status === 'ACTIVE'
                              ? 'success'
                              : camera.status === 'DECOMMISSIONED'
                                ? 'neutral'
                                : 'warn'
                          }
                        >
                          {camera.status}
                        </Badge>
                      </Td>
                      <Td className="text-xs tabular-nums">
                        {camera.installed_on || <span className="text-slate-600">unknown</span>}
                      </Td>
                      <Td className="text-xs tabular-nums text-slate-500">
                        {camera.updated_at ? camera.updated_at.slice(0, 10) : '—'}
                      </Td>
                      <Td>
                        <span className="inline-flex items-center gap-1.5 text-xs">
                          <StatusDot state={camera.connectivity_status} />
                          {camera.connectivity_status || 'UNKNOWN'}
                        </span>
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </div>
          )}
        </Card>

        {total > PAGE_SIZE && (
          <div className="flex items-center justify-between text-xs text-slate-400">
            <span className="tabular-nums">
              Showing {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, total)} of {total}
            </span>
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant="secondary"
                disabled={page === 0}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
              >
                Previous
              </Button>
              <span className="tabular-nums">
                Page {page + 1} of {pageCount}
              </span>
              <Button
                size="sm"
                variant="secondary"
                disabled={page + 1 >= pageCount}
                onClick={() => setPage((p) => p + 1)}
              >
                Next
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
