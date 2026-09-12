import React, { useEffect, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Plus, ShieldAlert, X } from 'lucide-react';

import { Button, Card, CardBody, CardHeader, ErrorState, Field, Input, Select, Spinner } from './ui/index.jsx';
import { api } from '../lib/api.js';

const PRIORITIES = ['P0', 'P1', 'P2', 'P3'];

/**
 * One-click path from "that plate, right there" to "watch for it everywhere".
 *
 * Posts to the same `/api/v1/watchlist` the Alerts page's watchlist panel
 * already uses — the worker reloads that table every few seconds, so a plate
 * flagged here starts raising alerts on its next sighting without a restart
 * or a deploy. Built once and shared by every entry point (the live feed, a
 * camera's recent-reads panel) rather than duplicated per page.
 */
export default function FlagSuspiciousDialog({ plate, onClose }) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState({
    classification: 'STOLEN_VEHICLE',
    priority: 'P0',
    reason: '',
    case_reference: '',
  });

  // A dialog reused across different plates must reset itself, not carry the
  // previous plate's half-typed reason into the next one.
  useEffect(() => {
    setForm({ classification: 'STOLEN_VEHICLE', priority: 'P0', reason: '', case_reference: '' });
  }, [plate]);

  const create = useMutation({
    mutationFn: (payload) => api.post('/api/v1/watchlist', payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['watchlist'] });
      onClose();
    },
  });

  const set = (key) => (event) => setForm((f) => ({ ...f, [key]: event.target.value }));

  if (!plate) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} aria-hidden />
      <Card className="relative w-full max-w-md border-accent/40">
        <CardHeader
          title={
            <span className="flex items-center gap-2">
              <ShieldAlert className="h-4 w-4 text-accent" /> Flag as suspicious
            </span>
          }
          description={`${plate} — starts matching on its next sighting, wherever it is read`}
          actions={
            <Button variant="ghost" size="icon" onClick={onClose} aria-label="Cancel">
              <X className="h-4 w-4" />
            </Button>
          }
        />
        <form
          onSubmit={(event) => {
            event.preventDefault();
            create.mutate({
              plate_number: plate,
              classification: form.classification,
              priority: form.priority,
              reason: form.reason,
              case_reference: form.case_reference || null,
            });
          }}
        >
          <CardBody className="space-y-3">
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
            <Field label="Reason" required hint="Why this plate is being watched">
              <Input
                required
                autoFocus
                value={form.reason}
                onChange={set('reason')}
                placeholder="Observed leaving the scene at..."
              />
            </Field>
            <Field label="Case reference" hint="Optional">
              <Input value={form.case_reference} onChange={set('case_reference')} placeholder="FIR-2026-00417" />
            </Field>
            {create.isError && <ErrorState error={create.error} />}
          </CardBody>
          <div className="flex justify-end gap-2 border-t border-ink-700 px-4 py-3">
            <Button type="button" variant="secondary" size="sm" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" size="sm" disabled={create.isPending}>
              {create.isPending ? <Spinner className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
              Add to watchlist
            </Button>
          </div>
        </form>
      </Card>
    </div>
  );
}
