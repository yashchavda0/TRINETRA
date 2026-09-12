import React, { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  Plus,
  ShieldAlert,
  Trash2,
  X,
} from 'lucide-react';

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
  Input,
  Select,
  Spinner,
  Table,
  Td,
  Th,
  Textarea,
  cn,
} from '../components/ui/index.jsx';
import { api } from '../lib/api.js';
import { useAuth } from '../lib/auth.jsx';

const PAGE_SIZE = 50;
const WORKFLOW_STATES = ['NEW', 'ACKNOWLEDGED', 'IN_PROGRESS', 'RESOLVED', 'FALSE_POSITIVE'];
const PRIORITIES = ['P0', 'P1', 'P2', 'P3'];

const PRIORITY_TONE = { P0: 'danger', P1: 'warn', P2: 'info', P3: 'neutral' };
const STATE_TONE = {
  NEW: 'danger',
  ACKNOWLEDGED: 'warn',
  IN_PROGRESS: 'info',
  RESOLVED: 'success',
  FALSE_POSITIVE: 'neutral',
};

function timeAgo(ms) {
  const seconds = Math.floor((Date.now() - ms) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

/* ------------------------------------------------------------------ */
/* Watchlist                                                           */
/* ------------------------------------------------------------------ */

function WatchlistPanel({ onClose }) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState({
    plate_number: '',
    classification: 'STOLEN_VEHICLE',
    priority: 'P0',
    reason: '',
    case_reference: '',
  });

  const entries = useQuery({
    queryKey: ['watchlist'],
    queryFn: () => api.get('/api/v1/watchlist'),
  });

  const create = useMutation({
    mutationFn: (payload) => api.post('/api/v1/watchlist', payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['watchlist'] });
      setForm((f) => ({ ...f, plate_number: '', reason: '', case_reference: '' }));
    },
  });

  const retire = useMutation({
    mutationFn: (id) => api.delete(`/api/v1/watchlist/${id}`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['watchlist'] }),
  });

  const set = (key) => (event) => setForm((f) => ({ ...f, [key]: event.target.value }));

  return (
    <Card className="border-accent/40">
      <CardHeader
        title="Watchlist"
        description="The worker reloads this every few seconds — a new entry starts matching almost immediately"
        actions={
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close watchlist">
            <X className="h-4 w-4" />
          </Button>
        }
      />

      <form
        onSubmit={(event) => {
          event.preventDefault();
          create.mutate({
            ...form,
            case_reference: form.case_reference || null,
          });
        }}
      >
        <CardBody className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <Field label="Plate" required hint="Spacing is normalised">
            <Input
              required
              value={form.plate_number}
              onChange={set('plate_number')}
              placeholder="GJ01AB1234"
            />
          </Field>
          <Field label="Classification">
            <Select value={form.classification} onChange={set('classification')}>
              <option value="STOLEN_VEHICLE">Stolen vehicle</option>
              <option value="WANTED_CRIMINAL">Wanted criminal</option>
              <option value="PERSON_OF_INTEREST">Person of interest</option>
            </Select>
          </Field>
          <Field label="Priority">
            <Select value={form.priority} onChange={set('priority')}>
              {PRIORITIES.map((p) => (
                <option key={p}>{p}</option>
              ))}
            </Select>
          </Field>
          <Field label="Reason" required className="sm:col-span-2 lg:col-span-1">
            <Input required value={form.reason} onChange={set('reason')} />
          </Field>
          <Field label="Case reference">
            <Input
              value={form.case_reference}
              onChange={set('case_reference')}
              placeholder="FIR-2026-00417"
            />
          </Field>
          {create.isError && (
            <div className="sm:col-span-2 lg:col-span-5">
              <ErrorState error={create.error} />
            </div>
          )}
        </CardBody>
        <div className="flex justify-end border-t border-ink-700 px-4 py-3">
          <Button type="submit" variant="primary" size="sm" disabled={create.isPending}>
            {create.isPending ? <Spinner className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
            Add to watchlist
          </Button>
        </div>
      </form>

      {entries.isLoading ? (
        <CardBody>
          <Spinner />
        </CardBody>
      ) : entries.data?.items?.length ? (
        <Table>
          <thead>
            <tr>
              <Th>Plate</Th>
              <Th>Classification</Th>
              <Th>Priority</Th>
              <Th>Reason</Th>
              <Th>Case</Th>
              <Th className="text-right">Actions</Th>
            </tr>
          </thead>
          <tbody>
            {entries.data.items.map((entry) => (
              <tr key={entry.id} className="hover:bg-ink-800/60">
                <Td className="font-mono text-xs">{entry.plate_number || '—'}</Td>
                <Td className="text-xs">{entry.classification}</Td>
                <Td>
                  <Badge tone={PRIORITY_TONE[entry.priority]}>{entry.priority}</Badge>
                </Td>
                <Td className="max-w-[260px] truncate text-xs">{entry.reason}</Td>
                <Td className="text-xs text-slate-400">{entry.case_reference || '—'}</Td>
                <Td className="text-right">
                  <Button
                    size="sm"
                    variant="ghost"
                    title="Retire entry"
                    onClick={() => retire.mutate(entry.id)}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      ) : (
        <EmptyState
          title="Watchlist is empty"
          description="Nothing is being watched for. Add a plate above to start matching."
        />
      )}
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/* Alert row                                                           */
/* ------------------------------------------------------------------ */

function AlertRow({ alert, expanded, onToggle }) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState('');

  const update = useMutation({
    mutationFn: (changes) => api.patch(`/api/v1/alerts/${alert.alert_id}`, changes),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['alerts'] }),
  });

  const isOpen = alert.workflow_state === 'NEW' || alert.workflow_state === 'ACKNOWLEDGED';

  return (
    <>
      <tr
        className={cn(
          'cursor-pointer hover:bg-ink-800/60',
          alert.workflow_state === 'NEW' && alert.priority === 'P0' && 'bg-dept-police/5',
        )}
        onClick={onToggle}
      >
        <Td>
          {expanded ? (
            <ChevronDown className="h-3.5 w-3.5 text-slate-500" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5 text-slate-600" />
          )}
        </Td>
        <Td>
          <Badge
            tone={PRIORITY_TONE[alert.priority]}
            className={cn(
              alert.priority === 'P0' &&
                alert.workflow_state === 'NEW' &&
                'animate-pulse-alert',
            )}
          >
            {alert.priority}
          </Badge>
        </Td>
        <Td className="font-mono text-xs">{alert.plate_number || '—'}</Td>
        <Td className="text-xs">{alert.classification}</Td>
        <Td className="text-xs">
          {alert.global_camera_code ? (
            <Link
              to={`/registry/${alert.camera_id}`}
              className="text-accent hover:underline"
              onClick={(event) => event.stopPropagation()}
            >
              {alert.global_camera_code}
            </Link>
          ) : (
            '—'
          )}
        </Td>
        <Td className="whitespace-nowrap text-xs text-slate-400">
          {timeAgo(alert.detected_at_utc_ms)}
        </Td>
        <Td>
          <Badge tone={STATE_TONE[alert.workflow_state]}>{alert.workflow_state}</Badge>
        </Td>
      </tr>

      {expanded && (
        <tr className="bg-ink-950/60">
          <Td colSpan={7} className="px-6 py-4">
            <div className="grid gap-4 lg:grid-cols-3">
              <div>
                <p className="mb-2 text-[11px] uppercase tracking-wide text-slate-500">Vehicle</p>
                <dl className="space-y-1 text-xs">
                  {Object.entries(alert.vehicle || {}).map(([key, value]) => (
                    <div key={key} className="flex justify-between gap-3">
                      <dt className="text-slate-500">{key}</dt>
                      <dd className="truncate text-slate-300">{String(value ?? '—')}</dd>
                    </div>
                  ))}
                  {Object.keys(alert.vehicle || {}).length === 0 && (
                    <p className="text-slate-600">No vehicle record attached.</p>
                  )}
                </dl>
              </div>

              <div>
                <p className="mb-2 text-[11px] uppercase tracking-wide text-slate-500">Subject</p>
                {alert.subject?.restricted ? (
                  <p className="flex items-start gap-1.5 text-xs text-slate-500">
                    <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    Identity data is restricted to department administrators.
                  </p>
                ) : (
                  <dl className="space-y-1 text-xs">
                    {Object.entries(alert.subject || {}).map(([key, value]) => (
                      <div key={key} className="flex justify-between gap-3">
                        <dt className="text-slate-500">{key}</dt>
                        <dd className="truncate text-slate-300">
                          {Array.isArray(value) ? value.join(', ') || '—' : String(value ?? '—')}
                        </dd>
                      </div>
                    ))}
                    {Object.keys(alert.subject || {}).length === 0 && (
                      <p className="text-slate-600">No subject record.</p>
                    )}
                  </dl>
                )}
              </div>

              <div>
                <p className="mb-2 text-[11px] uppercase tracking-wide text-slate-500">Evidence</p>
                <dl className="space-y-1 text-xs">
                  {Object.entries(alert.evidence || {}).map(([key, value]) => (
                    <div key={key} className="flex justify-between gap-3">
                      <dt className="text-slate-500">{key}</dt>
                      <dd className="truncate text-slate-300">{String(value ?? '—')}</dd>
                    </div>
                  ))}
                </dl>
                <p className="mt-2 font-mono text-[10px] text-slate-600">{alert.alert_id}</p>
              </div>
            </div>

            {isOpen && (
              <div className="mt-4 flex flex-wrap items-end gap-2 border-t border-ink-800 pt-3">
                {alert.workflow_state === 'NEW' && (
                  <Button
                    size="sm"
                    variant="secondary"
                    disabled={update.isPending}
                    onClick={() => update.mutate({ workflow_state: 'ACKNOWLEDGED' })}
                  >
                    <Check className="h-3.5 w-3.5" /> Acknowledge
                  </Button>
                )}
                <Field label="Resolution note" className="min-w-[240px] flex-1">
                  <Textarea
                    rows={1}
                    value={note}
                    onChange={(event) => setNote(event.target.value)}
                    placeholder="What was done about this alert?"
                  />
                </Field>
                <Button
                  size="sm"
                  variant="primary"
                  disabled={!note.trim() || update.isPending}
                  onClick={() =>
                    update.mutate({ workflow_state: 'RESOLVED', resolution_note: note.trim() })
                  }
                >
                  Resolve
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={!note.trim() || update.isPending}
                  onClick={() =>
                    update.mutate({
                      workflow_state: 'FALSE_POSITIVE',
                      resolution_note: note.trim(),
                    })
                  }
                >
                  False positive
                </Button>
              </div>
            )}

            {alert.resolution_note && (
              <p className="mt-3 rounded border border-ink-700 bg-ink-900 px-3 py-2 text-xs text-slate-300">
                <span className="text-slate-500">Resolution: </span>
                {alert.resolution_note}
              </p>
            )}

            {update.isError && (
              <div className="mt-3">
                <ErrorState error={update.error} />
              </div>
            )}
          </Td>
        </tr>
      )}
    </>
  );
}

/* ------------------------------------------------------------------ */
/* Page                                                                */
/* ------------------------------------------------------------------ */

export default function AlertsPage() {
  const { atLeast } = useAuth();
  const [filters, setFilters] = useState({ workflow_state: '', priority: '', plate: '' });
  const [page, setPage] = useState(0);
  const [expanded, setExpanded] = useState(null);
  const [showWatchlist, setShowWatchlist] = useState(false);

  const params = useMemo(
    () => ({
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
      ...Object.fromEntries(Object.entries(filters).filter(([, value]) => value)),
    }),
    [filters, page],
  );

  const alerts = useQuery({
    queryKey: ['alerts', params],
    queryFn: () => api.get('/api/v1/alerts', params),
    placeholderData: (previous) => previous,
    // An alert inbox that needs a manual refresh is not an inbox.
    refetchInterval: 15_000,
  });

  const total = alerts.data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const newCount = alerts.data?.items?.filter((a) => a.workflow_state === 'NEW').length ?? 0;

  return (
    <div className="min-h-full">
      <PageHeader
        title="Alerts"
        description="Threat alerts raised by watchlist matches and registry screening"
        actions={
          <>
            {newCount > 0 && <Badge tone="danger">{newCount} unacknowledged</Badge>}
            {atLeast('OPERATOR') && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setShowWatchlist((open) => !open)}
              >
                <ShieldAlert className="h-3.5 w-3.5" /> Watchlist
              </Button>
            )}
          </>
        }
      />

      <div className="space-y-4 p-4 sm:p-6">
        {showWatchlist && <WatchlistPanel onClose={() => setShowWatchlist(false)} />}

        <Card>
          <div className="grid gap-3 p-4 sm:grid-cols-3">
            <Field label="State">
              <Select
                value={filters.workflow_state}
                onChange={(event) => {
                  setFilters((f) => ({ ...f, workflow_state: event.target.value }));
                  setPage(0);
                }}
              >
                <option value="">All</option>
                {WORKFLOW_STATES.map((s) => (
                  <option key={s}>{s}</option>
                ))}
              </Select>
            </Field>
            <Field label="Priority">
              <Select
                value={filters.priority}
                onChange={(event) => {
                  setFilters((f) => ({ ...f, priority: event.target.value }));
                  setPage(0);
                }}
              >
                <option value="">All</option>
                {PRIORITIES.map((p) => (
                  <option key={p}>{p}</option>
                ))}
              </Select>
            </Field>
            <Field label="Plate">
              <Input
                value={filters.plate}
                onChange={(event) => {
                  setFilters((f) => ({ ...f, plate: event.target.value }));
                  setPage(0);
                }}
                placeholder="GJ01AB1234"
              />
            </Field>
          </div>
        </Card>

        <Card className="overflow-hidden">
          {alerts.isLoading ? (
            <CardBody>
              <Spinner />
            </CardBody>
          ) : alerts.isError ? (
            <CardBody>
              <ErrorState error={alerts.error} onRetry={alerts.refetch} />
            </CardBody>
          ) : total === 0 ? (
            <EmptyState
              icon={AlertTriangle}
              title="No alerts"
              description="Nothing has been raised in this scope. Alerts appear here the moment a watchlist plate is read or a registry screening returns a hit."
            />
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th className="w-8" />
                  <Th>Priority</Th>
                  <Th>Plate</Th>
                  <Th>Classification</Th>
                  <Th>Camera</Th>
                  <Th>Detected</Th>
                  <Th>State</Th>
                </tr>
              </thead>
              <tbody>
                {alerts.data.items.map((alert) => (
                  <AlertRow
                    key={alert.alert_id}
                    alert={alert}
                    expanded={expanded === alert.alert_id}
                    onToggle={() =>
                      setExpanded(expanded === alert.alert_id ? null : alert.alert_id)
                    }
                  />
                ))}
              </tbody>
            </Table>
          )}
        </Card>

        {total > PAGE_SIZE && (
          <div className="flex items-center justify-between text-xs text-slate-400">
            <span className="tabular-nums">
              {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, total)} of {total}
            </span>
            <div className="flex items-center gap-2">
              <Button
                size="sm"
                variant="secondary"
                disabled={page === 0}
                onClick={() => setPage((p) => p - 1)}
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
