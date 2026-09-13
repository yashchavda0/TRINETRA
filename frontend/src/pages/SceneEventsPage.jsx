import React, { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Camera } from 'lucide-react';

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

const PAGE_SIZE = 50;

const EVENT_TYPES = ['LOITERING', 'WRONG_WAY', 'COLLISION', 'CROWD_DENSITY', 'UNUSUAL_ACTIVITY'];

// Matches the alertable set workers/scene_event_worker.py fans out through
// the P0 alert channel - shown here only as a visual cue, not a filter, since
// every finding (alerted or not) belongs on this investigative list.
const ALERTABLE_TYPES = new Set(['COLLISION', 'WRONG_WAY', 'CROWD_DENSITY']);

function formatTime(ms) {
  return new Date(ms).toLocaleString();
}

function eventTypeTone(eventType) {
  if (eventType === 'COLLISION') return 'danger';
  if (ALERTABLE_TYPES.has(eventType)) return 'warn';
  return 'default';
}

export default function SceneEventsPage() {
  const [eventType, setEventType] = useState('');
  const [page, setPage] = useState(0);

  const params = useMemo(
    () => ({
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
      event_type: eventType || undefined,
    }),
    [eventType, page],
  );

  const sceneEvents = useQuery({
    queryKey: ['scene-events', params],
    queryFn: () => api.get('/api/v1/scene-events', params),
    placeholderData: (previous) => previous,
  });

  const total = sceneEvents.data?.total ?? 0;

  return (
    <div className="min-h-full">
      <PageHeader
        title="Scene events"
        description="Agentic (VLM) findings — loitering, wrong-way, collision, crowd density and open-vocabulary anomalies"
      />

      <div className="space-y-4 p-4 sm:p-6">
        <Card>
          <div className="grid gap-3 p-4 sm:grid-cols-[auto_1fr]">
            <Field label="Event type" className="w-48">
              <Select
                value={eventType}
                onChange={(event) => {
                  setEventType(event.target.value);
                  setPage(0);
                }}
              >
                <option value="">Any</option>
                {EVENT_TYPES.map((type) => (
                  <option key={type} value={type}>
                    {type}
                  </option>
                ))}
              </Select>
            </Field>
          </div>
        </Card>

        {sceneEvents.isError && <ErrorState error={sceneEvents.error} onRetry={sceneEvents.refetch} />}

        <Card className="overflow-hidden">
          <CardHeader
            title="Findings"
            description={sceneEvents.isFetching ? 'loading…' : `${total} finding(s)`}
          />
          {sceneEvents.isLoading ? (
            <CardBody>
              <Spinner />
            </CardBody>
          ) : total === 0 ? (
            <EmptyState
              icon={Camera}
              title="No scene events"
              description="services/vlm_agent's Tier B reasoning has not raised a finding yet. A finding appears here once a trigger (dwell time, crowd density) fires and the VLM confirms something notable."
            />
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>Type</Th>
                  <Th>Confidence</Th>
                  <Th>Camera</Th>
                  <Th>Site</Th>
                  <Th>Window</Th>
                  <Th>Rationale</Th>
                  <Th>Workflow</Th>
                </tr>
              </thead>
              <tbody>
                {sceneEvents.data.items.map((item) => (
                  <tr key={item.scene_event_id} className="hover:bg-ink-800/60">
                    <Td className="text-xs">
                      <Badge tone={eventTypeTone(item.event_type)}>{item.event_type}</Badge>
                    </Td>
                    <Td className="text-xs tabular-nums">{Math.round(item.confidence * 100)}%</Td>
                    <Td className="font-mono text-xs">
                      {item.global_camera_code || item.camera_id.slice(0, 8)}
                    </Td>
                    <Td className="max-w-[180px] truncate text-xs">{item.site_name || '—'}</Td>
                    <Td className="whitespace-nowrap text-xs text-slate-400">
                      {formatTime(item.window_start_utc_ms)}
                    </Td>
                    <Td className="max-w-[360px] text-xs text-slate-300">
                      {item.rationale || '—'}
                    </Td>
                    <Td className="text-xs text-slate-500">{item.workflow_state}</Td>
                  </tr>
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
              <Button
                size="sm"
                variant="secondary"
                disabled={(page + 1) * PAGE_SIZE >= total}
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
