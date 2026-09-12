import React, { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Grid2X2, Pause, Play } from 'lucide-react';

import LiveTile from '../components/LiveTile.jsx';
import { PageHeader } from '../components/layout/AppShell.jsx';
import {
  Button,
  EmptyState,
  ErrorState,
  Field,
  Input,
  Select,
  Spinner,
} from '../components/ui/index.jsx';
import { api } from '../lib/api.js';
import { useAuth } from '../lib/auth.jsx';
import { useQuery } from '@tanstack/react-query';

// Twelve tiles is a 3x4 wall, and twelve is also the concurrency budget: every
// tile is its own WebRTC session AND its own on-demand RTSP pull from the
// external grid, whose client rules say not to open cameras nobody is watching.
// Paging server-side rather than filtering a full fleet client-side is what
// keeps that promise - the other eighteen are never mounted, so they are never
// dialled.
const PAGE_SIZE = 12;

// The vendor written by scripts/register_grid_cameras.ps1. Rows from anywhere
// else may carry invented URLs (the AHM-TRF and BULK-TEST smoke-test cameras),
// which can only ever render a failure tile.
const STREAMABLE_VENDOR = 'LIVE-GRID';

const DEPARTMENTS = ['POLICE', 'RTO', 'GSRTC', 'CIVIL_SUPPLIES', 'REVENUE', 'PRIVATE'];

export default function VideoWallPage() {
  const navigate = useNavigate();
  const { departmentScope } = useAuth();

  const [search, setSearch] = useState('');
  const [debounced, setDebounced] = useState('');
  const [department, setDepartment] = useState('');
  const [streamableOnly, setStreamableOnly] = useState(true);
  const [page, setPage] = useState(0);
  const [playing, setPlaying] = useState(true);

  // Same debounce the registry uses: a keystroke per request would re-page the
  // wall - and therefore renegotiate twelve streams - on every letter.
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebounced(search.trim());
      setPage(0);
    }, 300);
    return () => clearTimeout(timer);
  }, [search]);

  const params = useMemo(
    () => ({
      status: 'ACTIVE',
      limit: PAGE_SIZE,
      offset: page * PAGE_SIZE,
      q: debounced || undefined,
      department_id: department || undefined,
      vms_vendor: streamableOnly ? STREAMABLE_VENDOR : undefined,
    }),
    [debounced, department, page, streamableOnly],
  );

  const cameras = useQuery({
    queryKey: ['cameras', 'wall', params],
    queryFn: () => api.get('/api/v1/cameras', params),
    // Without this the grid unmounts on every page change and every tile
    // restarts from scratch, including the ones that did not move.
    placeholderData: (previous) => previous,
  });

  const items = cameras.data?.items ?? [];
  const total = cameras.data?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="flex h-full min-h-full flex-col">
      <PageHeader
        title="Video wall"
        description={
          departmentScope
            ? `Live view of ${departmentScope} cameras, ${PAGE_SIZE} at a time.`
            : `Live view of the fleet, ${PAGE_SIZE} at a time.`
        }
        actions={
          <>
            <Field label="Search" className="w-48">
              <Input
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="code, site, ward…"
              />
            </Field>

            {/* Hidden for a scoped user: the server enforces the scope, and
                offering a filter that cannot widen it only misleads. */}
            {!departmentScope && (
              <Field label="Department" className="w-40">
                <Select
                  value={department}
                  onChange={(event) => {
                    setDepartment(event.target.value);
                    setPage(0);
                  }}
                >
                  <option value="">All</option>
                  {DEPARTMENTS.map((code) => (
                    <option key={code} value={code}>
                      {code}
                    </option>
                  ))}
                </Select>
              </Field>
            )}

            <Field label="Show" className="w-44">
              <Select
                value={streamableOnly ? 'streamable' : 'all'}
                onChange={(event) => {
                  setStreamableOnly(event.target.value === 'streamable');
                  setPage(0);
                }}
              >
                <option value="streamable">Streamable only</option>
                <option value="all">All active cameras</option>
              </Select>
            </Field>

            {/* Releases every grid connection without leaving the page. */}
            <Button
              variant={playing ? 'secondary' : 'primary'}
              onClick={() => setPlaying((current) => !current)}
            >
              {playing ? (
                <>
                  <Pause className="mr-1.5 h-4 w-4" aria-hidden />
                  Pause all
                </>
              ) : (
                <>
                  <Play className="mr-1.5 h-4 w-4" aria-hidden />
                  Resume all
                </>
              )}
            </Button>
          </>
        }
      />

      <div className="flex-1 overflow-y-auto p-4 sm:p-6">
        {cameras.isLoading ? (
          <div className="flex justify-center p-12">
            <Spinner className="h-6 w-6" />
          </div>
        ) : cameras.isError ? (
          <ErrorState error={cameras.error} onRetry={cameras.refetch} />
        ) : total === 0 ? (
          <EmptyState
            icon={Grid2X2}
            title="No cameras to show"
            description={
              streamableOnly
                ? 'No streamable cameras match this filter. Switch "Show" to all active cameras, or register the grid with scripts/register_grid_cameras.ps1.'
                : 'No active cameras match this filter.'
            }
          />
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {items.map((camera) => (
              <LiveTile
                // Keyed by camera so paging swaps the stream cleanly rather
                // than reusing a tile's peer connection for a new camera.
                key={camera.id}
                camera={camera}
                enabled={playing}
                onOpen={(target) => navigate(`/cameras/${target.id}`)}
              />
            ))}
          </div>
        )}
      </div>

      {total > 0 && (
        <div className="flex items-center justify-between border-t border-ink-700 bg-ink-850/60 px-4 py-2 sm:px-6">
          <p className="text-xs text-slate-500">
            {page * PAGE_SIZE + 1}–{Math.min((page + 1) * PAGE_SIZE, total)} of {total}
            {cameras.isFetching && <span className="ml-2">loading…</span>}
            {!playing && <span className="ml-2 text-state-warn">paused</span>}
          </p>
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              disabled={page === 0}
              onClick={() => setPage((current) => Math.max(0, current - 1))}
            >
              Previous
            </Button>
            <span className="text-xs text-slate-500">
              Page {page + 1} of {pageCount}
            </span>
            <Button
              size="sm"
              disabled={page + 1 >= pageCount}
              onClick={() => setPage((current) => current + 1)}
            >
              Next
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
