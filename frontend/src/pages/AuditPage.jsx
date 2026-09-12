import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { ChevronDown, ChevronRight, ShieldCheck } from 'lucide-react';

import { PageHeader } from '../components/layout/AppShell.jsx';
import {
  Badge,
  Button,
  Card,
  CardBody,
  EmptyState,
  ErrorState,
  Field,
  Input,
  Select,
  Spinner,
  Table,
  Td,
  Th,
} from '../components/ui/index.jsx';
import { api } from '../lib/api.js';

const PAGE_SIZE = 50;

/** Field-level diff for one audited change. */
function ChangeDetail({ entry }) {
  const fields = entry.changed.filter((field) => field !== 'updated_at');
  if (fields.length === 0) {
    return <p className="text-xs text-slate-500">No field-level detail recorded.</p>;
  }

  return (
    <div className="space-y-1">
      {fields.map((field) => (
        <div key={field} className="grid grid-cols-[9rem_1fr_1fr] gap-2 text-xs">
          <span className="truncate font-mono text-slate-500">{field}</span>
          <span className="truncate text-state-down/80 line-through">
            {entry.before?.[field] == null ? '—' : String(entry.before[field])}
          </span>
          <span className="truncate text-state-up">
            {entry.after?.[field] == null ? '—' : String(entry.after[field])}
          </span>
        </div>
      ))}
    </div>
  );
}

export default function AuditPage() {
  const [filters, setFilters] = useState({ entity: '', action: '', entity_id: '' });
  const [page, setPage] = useState(0);
  const [expanded, setExpanded] = useState(null);

  const params = {
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
    ...Object.fromEntries(Object.entries(filters).filter(([, value]) => value)),
  };

  const audit = useQuery({
    queryKey: ['audit', params],
    queryFn: () => api.get('/api/v1/audit', params),
    placeholderData: (previous) => previous,
  });

  const total = audit.data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="min-h-full">
      <PageHeader
        title="Audit trail"
        description="Every registry and account change, written by a database trigger"
      />

      <div className="space-y-4 p-4 sm:p-6">
        <div className="flex items-start gap-2 rounded-md border border-ink-700 bg-ink-850 px-3 py-2 text-xs text-slate-400">
          <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-500" aria-hidden />
          <p>
            The trail is append-only and has no edit or delete endpoint. Entries are produced by a
            trigger on the table itself, so a change made outside this console — a direct SQL
            session, a script — is recorded too, with the actor left blank rather than forged.
            Password hashes are stripped before anything is written here.
          </p>
        </div>

        <Card>
          <div className="grid gap-3 p-4 sm:grid-cols-3">
            <Field label="Entity">
              <Select
                value={filters.entity}
                onChange={(event) => {
                  setFilters((f) => ({ ...f, entity: event.target.value }));
                  setPage(0);
                }}
              >
                <option value="">All</option>
                <option value="cameras">Cameras</option>
                <option value="users">Users</option>
              </Select>
            </Field>
            <Field label="Action">
              <Select
                value={filters.action}
                onChange={(event) => {
                  setFilters((f) => ({ ...f, action: event.target.value }));
                  setPage(0);
                }}
              >
                <option value="">All</option>
                <option value="INSERT">Created</option>
                <option value="UPDATE">Updated</option>
                <option value="DELETE">Deleted</option>
              </Select>
            </Field>
            <Field label="Entity id" hint="Paste a camera or user UUID">
              <Input
                value={filters.entity_id}
                onChange={(event) => {
                  setFilters((f) => ({ ...f, entity_id: event.target.value.trim() }));
                  setPage(0);
                }}
                placeholder="optional"
              />
            </Field>
          </div>
        </Card>

        <Card className="overflow-hidden">
          {audit.isLoading ? (
            <CardBody>
              <Spinner />
            </CardBody>
          ) : audit.isError ? (
            <CardBody>
              <ErrorState error={audit.error} onRetry={audit.refetch} />
            </CardBody>
          ) : audit.data?.items?.length ? (
            <Table>
              <thead>
                <tr>
                  <Th className="w-8" />
                  <Th>When</Th>
                  <Th>Entity</Th>
                  <Th>Action</Th>
                  <Th>Changed fields</Th>
                  <Th>Actor</Th>
                </tr>
              </thead>
              <tbody>
                {audit.data.items.map((entry) => {
                  const isOpen = expanded === entry.id;
                  return (
                    <React.Fragment key={entry.id}>
                      <tr
                        className="cursor-pointer hover:bg-ink-800/60"
                        onClick={() => setExpanded(isOpen ? null : entry.id)}
                      >
                        <Td>
                          {isOpen ? (
                            <ChevronDown className="h-3.5 w-3.5 text-slate-500" />
                          ) : (
                            <ChevronRight className="h-3.5 w-3.5 text-slate-600" />
                          )}
                        </Td>
                        <Td className="whitespace-nowrap text-xs tabular-nums text-slate-400">
                          {new Date(entry.at).toLocaleString()}
                        </Td>
                        <Td className="text-xs">
                          {entry.entity}
                          <span className="ml-1 font-mono text-[10px] text-slate-600">
                            {entry.entity_id.slice(0, 8)}
                          </span>
                        </Td>
                        <Td>
                          <Badge
                            tone={
                              entry.action === 'INSERT'
                                ? 'success'
                                : entry.action === 'DELETE'
                                  ? 'danger'
                                  : 'info'
                            }
                          >
                            {entry.action}
                          </Badge>
                        </Td>
                        <Td className="max-w-[280px] truncate text-xs text-slate-400">
                          {entry.changed.filter((f) => f !== 'updated_at').join(', ') || '—'}
                        </Td>
                        <Td className="text-xs text-slate-400">
                          {entry.actor_label || (
                            <span className="text-slate-600">outside the console</span>
                          )}
                        </Td>
                      </tr>
                      {isOpen && (
                        <tr className="bg-ink-950/60">
                          <Td colSpan={6} className="px-6 py-3">
                            <ChangeDetail entry={entry} />
                          </Td>
                        </tr>
                      )}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </Table>
          ) : (
            <EmptyState title="No audit entries" description="No changes match these filters." />
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
