import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ArrowRight, Camera, Info, MapPin, Radio, Search, ShieldAlert } from 'lucide-react';

import FlagSuspiciousDialog from '../components/FlagSuspiciousDialog.jsx';

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
  cn,
} from '../components/ui/index.jsx';
import { api } from '../lib/api.js';

const PAGE_SIZE = 50;

function formatTime(ms) {
  return new Date(ms).toLocaleString();
}

const LIVE_TOKEN = import.meta.env.VITE_P0_ALERT_TOKEN || null;
const LIVE_MAX_READS = 100;

/* ------------------------------------------------------------------ */
/* Live feed — /detections/live                                        */
/* ------------------------------------------------------------------ */

// Same shared secret and reconnect-with-backoff shape as the P0 alert socket
// in GISMap.jsx: this is the only other place a console holds a live push
// channel, and there is no reason for the two to diverge.
function useLiveDetections(onRead) {
  const [status, setStatus] = useState('connecting');
  const onReadRef = useRef(onRead);
  onReadRef.current = onRead;

  useEffect(() => {
    let disposed = false;
    let attempt = 0;
    let socket;
    let reconnectTimer;

    const socketUrl = () => {
      const url = new URL('/detections/live', window.location.href);
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
      if (LIVE_TOKEN) url.searchParams.set('token', LIVE_TOKEN);
      return url.toString();
    };

    const scheduleReconnect = () => {
      if (disposed) return;
      const delay = Math.min(1000 * 2 ** attempt++, 30000) * (0.75 + Math.random() * 0.5);
      reconnectTimer = window.setTimeout(connect, delay);
    };

    function connect() {
      if (disposed) return;
      try {
        socket = new WebSocket(socketUrl());
      } catch {
        scheduleReconnect();
        return;
      }

      socket.onopen = () => {
        attempt = 0;
        setStatus('live');
      };
      socket.onmessage = (event) => {
        try {
          const frame = JSON.parse(event.data);
          if (frame && typeof frame === 'object') onReadRef.current(frame);
        } catch {
          // Malformed frame: skip it, the connection itself is still healthy.
        }
      };
      socket.onerror = () => setStatus('error');
      socket.onclose = (event) => {
        // The API refused the credentials; retrying cannot fix that.
        if (event.code === 1008) {
          setStatus('unauthorised - check VITE_P0_ALERT_TOKEN');
          return;
        }
        setStatus('reconnecting');
        scheduleReconnect();
      };
    }

    connect();
    return () => {
      disposed = true;
      window.clearTimeout(reconnectTimer);
      if (socket) {
        socket.onclose = null;
        socket.close();
      }
    };
  }, []);

  return status;
}

function LiveFeedPanel({ reads, status, selectedPlate, onSelectPlate, onFlag }) {
  return (
    <Card>
      <CardHeader
        title="Live feed"
        description="Plate reads as the ANPR service publishes them — no refresh needed"
        actions={
          <Badge tone={status === 'live' ? 'success' : status === 'connecting' ? 'default' : 'warn'}>
            <Radio className="h-3 w-3" /> {status}
          </Badge>
        }
      />
      <CardBody>
        {reads.length === 0 ? (
          <p className="py-2 text-xs text-slate-500">
            Waiting for the next plate read. Every sampled frame that yields one lands here
            within a second or two.
          </p>
        ) : (
          <div className="flex flex-wrap gap-2">
            {reads.map((read) => (
              <div
                key={read.event_id}
                className={cn(
                  'flex items-center gap-2 rounded-md border px-3 py-1.5 text-xs transition-colors',
                  selectedPlate === read.plate_number
                    ? 'border-accent bg-accent/15 text-slate-100'
                    : 'border-ink-700 text-slate-300 hover:bg-ink-800',
                )}
              >
                <button
                  type="button"
                  onClick={() => onSelectPlate(read.plate_number)}
                  className="flex items-center gap-2"
                >
                  <span className="font-mono">{read.plate_number}</span>
                  {read.plate_confidence != null && (
                    <span className="tabular-nums text-slate-500">
                      {Math.round(read.plate_confidence * 100)}%
                    </span>
                  )}
                  <span className="text-slate-500">
                    {read.global_camera_code || String(read.camera_id).slice(0, 8)}
                  </span>
                  <span className="tabular-nums text-slate-600">{formatTime(read.timestamp_utc_ms)}</span>
                </button>
                <button
                  type="button"
                  title="Flag as suspicious"
                  onClick={() => onFlag(read.plate_number)}
                  className="text-slate-500 hover:text-dept-police"
                >
                  <ShieldAlert className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>
        )}
      </CardBody>
    </Card>
  );
}

function formatDuration(seconds) {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`;
}

/* ------------------------------------------------------------------ */
/* Movement timeline                                                   */
/* ------------------------------------------------------------------ */

function MovementTimeline({ plate, onFlag }) {
  const history = useQuery({
    queryKey: ['movements', plate],
    queryFn: () => api.get(`/api/v1/detections/plate/${encodeURIComponent(plate)}/movements`),
    enabled: Boolean(plate),
  });

  if (!plate) return null;

  if (history.isLoading) {
    return (
      <Card>
        <CardBody>
          <Spinner />
        </CardBody>
      </Card>
    );
  }

  if (history.isError) {
    return (
      <Card>
        <CardBody>
          <ErrorState error={history.error} onRetry={history.refetch} />
        </CardBody>
      </Card>
    );
  }

  const data = history.data;

  if (!data || data.sighting_count === 0) {
    return (
      <Card>
        <EmptyState
          icon={Camera}
          title={`No sightings of ${plate}`}
          description="No registered camera has read this plate in your scope."
        />
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader
        title={`Movement history — ${data.plate_number}`}
        description={`${data.sighting_count} sighting(s) across ${data.distinct_cameras} camera(s)`}
        actions={
          <div className="flex items-center gap-2">
            <Button size="sm" variant="secondary" onClick={() => onFlag(data.plate_number)}>
              <ShieldAlert className="h-3.5 w-3.5" /> Flag as suspicious
            </Button>
            <Link to={`/map?plate=${encodeURIComponent(data.plate_number)}`}>
              <Button size="sm" variant="secondary">
                <MapPin className="h-3.5 w-3.5" /> Plot on map
              </Button>
            </Link>
          </div>
        }
      />

      <CardBody className="space-y-4">
        <div className="flex items-start gap-2 rounded-md border border-accent/30 bg-accent/10 px-3 py-2 text-xs text-slate-300">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent" aria-hidden />
          <p>{data.caveat}</p>
        </div>

        <ol className="relative space-y-0 border-l border-ink-700 pl-6">
          {data.sightings.map((sighting, index) => {
            // The hop that *departs* this sighting, when there is one.
            const hop = data.hops.find(
              (h) => h.departed_utc_ms === sighting.timestamp_utc_ms,
            );
            const isLast = index === data.sightings.length - 1;

            return (
              <li key={sighting.event_id} className="relative pb-6 last:pb-0">
                <span
                  className={cn(
                    'absolute -left-[1.72rem] top-1 h-3 w-3 rounded-full border-2 border-ink-900',
                    isLast ? 'bg-dept-police' : 'bg-accent',
                  )}
                />
                <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <Link
                    to={`/registry/${sighting.camera_id}`}
                    className="font-mono text-xs text-accent hover:underline"
                  >
                    {sighting.global_camera_code || sighting.camera_id.slice(0, 8)}
                  </Link>
                  {sighting.site_name && (
                    <span className="text-xs text-slate-300">{sighting.site_name}</span>
                  )}
                  <span className="text-xs tabular-nums text-slate-500">
                    {formatTime(sighting.timestamp_utc_ms)}
                  </span>
                  {sighting.plate_confidence != null && (
                    <Badge tone={sighting.plate_confidence >= 0.8 ? 'success' : 'warn'}>
                      {Math.round(sighting.plate_confidence * 100)}% read
                    </Badge>
                  )}
                  {isLast && <Badge tone="danger">most recent</Badge>}
                </div>

                {hop && (
                  <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
                    <ArrowRight className="h-3 w-3" aria-hidden />
                    <span>{formatDuration(hop.transit_seconds)} to {hop.to_camera_code}</span>
                    {hop.distance_meters != null && (
                      <span className="tabular-nums">· {Math.round(hop.distance_meters)} m apart</span>
                    )}
                    {hop.implied_speed_kmh != null && hop.implied_speed_kmh > 0 && (
                      <span
                        className={cn(
                          'tabular-nums',
                          hop.implied_speed_kmh > 120 && 'text-state-warn',
                        )}
                      >
                        · {hop.implied_speed_kmh} km/h straight-line
                      </span>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ol>
      </CardBody>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/* Page                                                                */
/* ------------------------------------------------------------------ */

export default function VehicleSearchPage() {
  const [query, setQuery] = useState('');
  const [submitted, setSubmitted] = useState('');
  const [objectClass, setObjectClass] = useState('');
  const [page, setPage] = useState(0);

  const params = useMemo(
    () => ({
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
      plates_only: true,
      plate_like: submitted || undefined,
      object_class: objectClass || undefined,
    }),
    [submitted, objectClass, page],
  );

  const detections = useQuery({
    queryKey: ['detections', params],
    queryFn: () => api.get('/api/v1/detections', params),
    placeholderData: (previous) => previous,
  });

  // Distinct plates in the result set, so the operator picks which vehicle to
  // trace rather than getting a timeline for a partial match.
  const plates = useMemo(() => {
    const seen = new Map();
    for (const item of detections.data?.items ?? []) {
      if (!item.plate_number) continue;
      const existing = seen.get(item.plate_number);
      if (!existing || item.timestamp_utc_ms > existing.last_seen) {
        seen.set(item.plate_number, {
          plate: item.plate_number,
          last_seen: item.timestamp_utc_ms,
          count: (existing?.count ?? 0) + 1,
        });
      } else {
        existing.count += 1;
      }
    }
    return [...seen.values()].sort((a, b) => b.last_seen - a.last_seen);
  }, [detections.data]);

  const [selectedPlate, setSelectedPlate] = useState(null);
  const total = detections.data?.total ?? 0;

  // Live reads, independent of the search filters above: this is what makes
  // "the live feed is always coming" visible on the page, not only true in
  // the database.
  const [liveReads, setLiveReads] = useState([]);
  const handleLiveRead = useCallback((frame) => {
    if (!frame.plate_number) return; // a Re-ID-only detection, nothing to show here
    setLiveReads((previous) => {
      if (previous.some((r) => r.event_id === frame.event_id)) return previous;
      return [frame, ...previous].slice(0, LIVE_MAX_READS);
    });
  }, []);
  const liveStatus = useLiveDetections(handleLiveRead);

  const [flagPlate, setFlagPlate] = useState(null);

  return (
    <div className="min-h-full">
      <PageHeader
        title="Vehicle search"
        description="Plate reads from ANPR, and where each vehicle has been seen"
      />

      <div className="space-y-4 p-4 sm:p-6">
        <LiveFeedPanel
          reads={liveReads}
          status={liveStatus}
          selectedPlate={selectedPlate}
          onSelectPlate={(plate) => setSelectedPlate((current) => (current === plate ? null : plate))}
          onFlag={setFlagPlate}
        />

        {flagPlate && <FlagSuspiciousDialog plate={flagPlate} onClose={() => setFlagPlate(null)} />}

        <Card>
          <form
            className="grid gap-3 p-4 sm:grid-cols-[1fr_auto_auto]"
            onSubmit={(event) => {
              event.preventDefault();
              setSubmitted(query.trim());
              setSelectedPlate(null);
              setPage(0);
            }}
          >
            <Field label="Plate number" hint="Partial matches allowed — useful for a misread character">
              <div className="relative">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-slate-500" />
                <Input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="GJ01AB1234 or just AB12"
                  className="pl-8"
                />
              </div>
            </Field>
            <Field label="Object class" className="w-40">
              <Select value={objectClass} onChange={(event) => setObjectClass(event.target.value)}>
                <option value="">Any</option>
                {['AUTOMOBILE', 'MOTORCYCLE', 'TRUCK', 'BUS'].map((c) => (
                  <option key={c}>{c}</option>
                ))}
              </Select>
            </Field>
            <div className="flex items-end">
              <Button type="submit" variant="primary">
                Search
              </Button>
            </div>
          </form>
        </Card>

        {detections.isError && <ErrorState error={detections.error} onRetry={detections.refetch} />}

        {plates.length > 0 && (
          <Card>
            <CardHeader
              title="Matching vehicles"
              description="Select one to trace its movement"
            />
            <CardBody className="flex flex-wrap gap-2">
              {plates.map((entry) => (
                <button
                  key={entry.plate}
                  type="button"
                  onClick={() =>
                    setSelectedPlate(selectedPlate === entry.plate ? null : entry.plate)
                  }
                  className={cn(
                    'rounded-md border px-3 py-1.5 text-xs font-mono transition-colors',
                    selectedPlate === entry.plate
                      ? 'border-accent bg-accent/15 text-slate-100'
                      : 'border-ink-700 text-slate-300 hover:bg-ink-800',
                  )}
                >
                  {entry.plate}
                  <span className="ml-2 font-sans text-[10px] text-slate-500">
                    {entry.count} read{entry.count === 1 ? '' : 's'}
                  </span>
                </button>
              ))}
            </CardBody>
          </Card>
        )}

        {selectedPlate && <MovementTimeline plate={selectedPlate} onFlag={setFlagPlate} />}

        <Card className="overflow-hidden">
          <CardHeader
            title="Plate reads"
            description={detections.isFetching ? 'loading…' : `${total} reading(s)`}
          />
          {detections.isLoading ? (
            <CardBody>
              <Spinner />
            </CardBody>
          ) : total === 0 ? (
            <EmptyState
              icon={Camera}
              title="No plate reads"
              description={
                submitted
                  ? `Nothing matching "${submitted}". ANPR reads appear here within seconds of being published.`
                  : 'No plates have been read yet. The ANPR service publishes readings onto the analytics bus, and they land here.'
              }
            />
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>Plate</Th>
                  <Th>Confidence</Th>
                  <Th>Camera</Th>
                  <Th>Site</Th>
                  <Th>Seen</Th>
                  <Th>Class</Th>
                </tr>
              </thead>
              <tbody>
                {detections.data.items.map((item) => (
                  <tr
                    key={item.event_id}
                    className="cursor-pointer hover:bg-ink-800/60"
                    onClick={() => setSelectedPlate(item.plate_number)}
                  >
                    <Td className="font-mono text-xs text-accent">{item.plate_number}</Td>
                    <Td className="text-xs tabular-nums">
                      {item.plate_confidence != null
                        ? `${Math.round(item.plate_confidence * 100)}%`
                        : '—'}
                    </Td>
                    <Td className="font-mono text-xs">
                      {item.global_camera_code || item.camera_id.slice(0, 8)}
                    </Td>
                    <Td className="max-w-[220px] truncate text-xs">{item.site_name || '—'}</Td>
                    <Td className="whitespace-nowrap text-xs text-slate-400">
                      {formatTime(item.timestamp_utc_ms)}
                    </Td>
                    <Td className="text-xs text-slate-500">{item.object_class}</Td>
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
