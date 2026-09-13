import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { ChevronLeft, ChevronRight, Film, SkipBack, SkipForward } from 'lucide-react';

import PlateSnapshot from '../components/PlateSnapshot.jsx';
import { PageHeader } from '../components/layout/AppShell.jsx';
import {
  Badge,
  Button,
  Card,
  CardBody,
  EmptyState,
  ErrorState,
  Field,
  Select,
  Spinner,
} from '../components/ui/index.jsx';
import { api } from '../lib/api.js';

// A detection's bbox is only valid at its own instant - there is no per-frame
// tracking here, one bbox per read. The overlay is shown for a short window
// either side of that instant so a fast-moving vehicle doesn't visibly slide
// out from under a box that stopped updating; wide enough to be findable by
// eye, narrow enough not to claim continuous tracking it doesn't have.
const BBOX_TOLERANCE_MS = 1500;

// One hour of timeline on screen at a time. Segments roll every
// RECORDING_SEGMENT_SECONDS (15 min server-side default), so an hour is
// enough to see several without the bar becoming unreadably dense.
const WINDOW_MS = 60 * 60 * 1000;
// How much to actually fetch/play around a clicked point: long enough to see
// what happened, short enough that a click never downloads more than this.
const CLIP_SECONDS = 60;

function formatClock(date) {
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

/**
 * Horizontal availability bar: which segments exist, and where a plate was
 * read. Both are drawn against the same [windowStart, windowEnd] scale so a
 * detection tick lines up with the segment it actually falls inside.
 */
function Timeline({
  windowStart,
  windowEnd,
  segments,
  detections,
  selectedStart,
  onPick,
  onPickDetection,
}) {
  const span = windowEnd.getTime() - windowStart.getTime();
  const pctOf = (ms) => Math.min(100, Math.max(0, ((ms - windowStart.getTime()) / span) * 100));

  return (
    <div className="space-y-2">
      <div className="relative h-10 rounded-md bg-ink-800">
        {segments.map((segment) => {
          const start = new Date(segment.start).getTime();
          const end = start + segment.duration_seconds * 1000;
          const isSelected = selectedStart === segment.start;
          const left = pctOf(start);
          const width = Math.max(0.4, pctOf(end) - left); // a floor so a short segment stays clickable
          return (
            <button
              key={segment.start}
              type="button"
              onClick={() => onPick(segment)}
              title={`${new Date(start).toLocaleString()} (${Math.round(segment.duration_seconds)}s)`}
              className={
                'absolute top-1 bottom-1 rounded-sm transition-colors ' +
                (isSelected ? 'bg-accent' : 'bg-ink-600 hover:bg-ink-500')
              }
              style={{ left: `${left}%`, width: `${width}%` }}
            />
          );
        })}
        {/* Detection ticks sit above the segment row so a plate read is
            visible even when its segment is thin at this zoom level. Buttons,
            not decoration: clicking one seeks the player to that instant. */}
        {detections.map((detection) => (
          <button
            key={detection.event_id}
            type="button"
            onClick={() => onPickDetection(detection)}
            className="absolute -top-1.5 h-2 w-2 -translate-x-1/2 rotate-45 bg-state-warn hover:scale-125"
            style={{ left: `${pctOf(detection.timestamp_utc_ms)}%` }}
            title={`${detection.plate_number || 'detection'} at ${new Date(detection.timestamp_utc_ms).toLocaleTimeString()}`}
          />
        ))}
      </div>
      <div className="flex justify-between text-[11px] text-slate-500">
        <span>{formatClock(windowStart)}</span>
        <span>{formatClock(windowEnd)}</span>
      </div>
    </div>
  );
}

export default function PlaybackPage() {
  const [params, setParams] = useSearchParams();
  const cameraId = params.get('camera') || '';
  const atMs = params.get('at') ? Number(params.get('at')) : null;

  // Centre the window on `at` when arriving from a deep link (Vehicle
  // Search's "View clip"); otherwise show the last hour up to now.
  const [windowEnd, setWindowEnd] = useState(
    () => new Date((atMs ?? Date.now()) + WINDOW_MS / 2),
  );
  const windowStart = new Date(windowEnd.getTime() - WINDOW_MS);

  const [selected, setSelected] = useState(null); // the RecordingSegment currently loaded
  const videoRef = useRef(null);
  const consumedDeepLinkRef = useRef(false);
  // A wall-clock instant to seek to once the video's metadata is ready -
  // needed because assigning a new blob: URL means the element has to reload
  // before currentTime can be set, so a seek requested at the same moment a
  // segment is picked has to wait for onLoadedMetadata to actually apply.
  const pendingSeekMsRef = useRef(null);
  // The detection nearest the video's current playback position, within
  // BBOX_TOLERANCE_MS - what the overlay box and the "captured frame" panel
  // are drawn from. Null whenever nothing is that close, which is most of any
  // clip: a detection is an instant, not a span.
  const [activeDetection, setActiveDetection] = useState(null);

  const cameras = useQuery({
    queryKey: ['cameras', 'playback-picker'],
    queryFn: () => api.get('/api/v1/cameras', { status: 'ACTIVE', limit: 500 }),
  });

  const recordings = useQuery({
    queryKey: ['recordings', cameraId, windowStart.getTime(), windowEnd.getTime()],
    queryFn: () =>
      api.get(`/api/v2/streams/${cameraId}/recordings`, {
        start: windowStart.toISOString(),
        end: windowEnd.toISOString(),
      }),
    enabled: Boolean(cameraId),
  });

  // The detections table already has a scope-aware, department-filtered read
  // path built for exactly this query - reused rather than duplicated inside
  // the recordings endpoint. Plates only: that is what "vehicle detected"
  // means anywhere in this codebase today.
  const detections = useQuery({
    queryKey: ['detections', 'playback', cameraId, windowStart.getTime(), windowEnd.getTime()],
    queryFn: () =>
      api.get('/api/v1/detections', {
        camera_id: cameraId,
        since_utc_ms: windowStart.getTime(),
        until_utc_ms: windowEnd.getTime(),
        plates_only: true,
        limit: 200,
      }),
    enabled: Boolean(cameraId),
  });

  const segments = recordings.data?.segments || [];
  const plates = detections.data?.items || [];

  // Set only when a deep link's exact moment falls in a real gap - the RTSP
  // source was briefly down (this grid drops and reconnects) or recording had
  // not started yet for this camera. Distinct from "nothing selected": this
  // means a specific moment was asked for and genuinely is not on disk, so the
  // fallback segment shown instead must not be mistaken for it.
  const [missedDeepLink, setMissedDeepLink] = useState(false);

  // Auto-pick a segment once the window's segments load: the one containing
  // `at` on first arrival from a deep link, otherwise the most recent.
  useEffect(() => {
    if (!segments.length) return;
    if (atMs && !consumedDeepLinkRef.current) {
      consumedDeepLinkRef.current = true;
      const hit = segments.find((segment) => {
        const start = new Date(segment.start).getTime();
        return atMs >= start && atMs <= start + segment.duration_seconds * 1000;
      });
      if (hit) {
        pendingSeekMsRef.current = atMs;
        setSelected(hit);
        setMissedDeepLink(false);
        return;
      }
      setMissedDeepLink(true);
    }
    setSelected((current) => current ?? segments[segments.length - 1]);
  }, [segments, atMs]);

  const clipUrl = useMemo(() => {
    if (!cameraId || !selected) return null;
    const query = new URLSearchParams({
      start: selected.start,
      duration_seconds: String(Math.min(selected.duration_seconds || CLIP_SECONDS, CLIP_SECONDS)),
    });
    return `/api/v2/streams/${cameraId}/clip?${query}`;
  }, [cameraId, selected]);

  const [clipError, setClipError] = useState(null);

  // A <video src="..."> is a plain browser GET with no way to attach our
  // bearer token - unlike every other call in this console, which goes
  // through api.js. /api/v2 requires auth, so pointing the element straight
  // at clipUrl would 401. Fetching it ourselves (Authorization included) and
  // handing the element a blob: URL keeps the same auth model as everywhere
  // else, with no token ever riding in a URL. Clips are capped at
  // CLIP_SECONDS, so buffering one response is a few MB at most - the
  // streaming/seeking a raw src enables is not something this fixed-window
  // player needs.
  useEffect(() => {
    if (!clipUrl) return undefined;
    let objectUrl = null;
    let cancelled = false;

    setClipError(null);
    setActiveDetection(null);
    api
      .raw(clipUrl)
      .then((response) => response.blob())
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        if (videoRef.current) {
          videoRef.current.src = objectUrl;
          videoRef.current.load();
        }
      })
      .catch((error) => {
        if (!cancelled) setClipError(error.detail || error.message);
      });

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [clipUrl]);

  // Once the freshly-loaded clip actually has a duration, apply any pending
  // seek (a deep link, or a detection clicked on the timeline). Cannot be done
  // eagerly - a blob: URL only becomes seekable after the element loads it.
  const handleLoadedMetadata = () => {
    const video = videoRef.current;
    const pendingMs = pendingSeekMsRef.current;
    if (!video || pendingMs == null || !selected) return;
    const offsetSeconds = (pendingMs - new Date(selected.start).getTime()) / 1000;
    video.currentTime = Math.min(Math.max(offsetSeconds, 0), video.duration || offsetSeconds);
    pendingSeekMsRef.current = null;
  };

  // Which detection (if any) the currently playing instant is close enough to
  // for the overlay box to mean something. Driven by the video's own clock,
  // not a timer, so it stays exactly in step with what is on screen.
  const handleTimeUpdate = () => {
    const video = videoRef.current;
    if (!video || !selected) return;
    const wallClockMs = new Date(selected.start).getTime() + video.currentTime * 1000;
    const nearest = plates.reduce((best, detection) => {
      const delta = Math.abs(detection.timestamp_utc_ms - wallClockMs);
      if (delta > BBOX_TOLERANCE_MS) return best;
      if (!best || delta < Math.abs(best.timestamp_utc_ms - wallClockMs)) return detection;
      return best;
    }, null);
    setActiveDetection((current) => (current?.event_id === nearest?.event_id ? current : nearest));
  };

  /** Select (if needed) the segment containing a detection, then seek to it. */
  const seekToDetection = (detection) => {
    const targetMs = detection.timestamp_utc_ms;
    const hit = segments.find((segment) => {
      const start = new Date(segment.start).getTime();
      return targetMs >= start && targetMs <= start + segment.duration_seconds * 1000;
    });
    if (!hit) return; // no footage for this detection - nothing to seek to

    pendingSeekMsRef.current = targetMs;
    if (selected?.start === hit.start && videoRef.current?.readyState >= 1) {
      // Same segment already loaded: onLoadedMetadata will not fire again, so
      // apply the seek immediately instead of waiting for an event that is
      // not coming.
      handleLoadedMetadata();
    } else {
      setSelected(hit);
    }
  };

  const hasBBox = (detection) =>
    detection &&
    detection.bbox_x_min != null &&
    detection.bbox_y_min != null &&
    detection.bbox_x_max != null &&
    detection.bbox_y_max != null;

  const jumpToDetection = (direction) => {
    if (!plates.length) return;
    // Prefer stepping from wherever the video actually is, not just which
    // segment is loaded, so repeated clicks move past a detection just seen
    // rather than bouncing back to the start of the current segment.
    const reference =
      videoRef.current && selected
        ? new Date(selected.start).getTime() + videoRef.current.currentTime * 1000
        : selected
          ? new Date(selected.start).getTime()
          : windowEnd.getTime();
    const ordered = [...plates].sort((a, b) => a.timestamp_utc_ms - b.timestamp_utc_ms);
    const next =
      direction > 0
        ? ordered.find((d) => d.timestamp_utc_ms > reference)
        : [...ordered].reverse().find((d) => d.timestamp_utc_ms < reference);
    if (next) seekToDetection(next);
  };

  return (
    <div className="flex h-full flex-col">
      <PageHeader
        title="Recording & playback"
        description="Recorded footage per camera, with plate detections highlighted on the timeline"
        actions={
          <Field label="Camera" className="w-72">
            <Select
              value={cameraId}
              onChange={(event) => {
                consumedDeepLinkRef.current = false;
                setSelected(null);
                setMissedDeepLink(false);
                setParams(event.target.value ? { camera: event.target.value } : {});
              }}
            >
              <option value="">Select a camera…</option>
              {(cameras.data?.items || []).map((camera) => (
                <option key={camera.id} value={camera.id}>
                  {camera.global_camera_code}
                  {camera.site_name ? ` — ${camera.site_name}` : ''}
                </option>
              ))}
            </Select>
          </Field>
        }
      />

      <div className="flex-1 space-y-4 overflow-y-auto p-4 sm:p-6">
        {!cameraId ? (
          <EmptyState
            icon={Film}
            title="Pick a camera"
            description="Choose a camera above, or open a clip from a sighting in Vehicle Search."
          />
        ) : (
          <>
            <Card>
              <CardBody>
                <div className="grid gap-3 lg:grid-cols-[1fr_auto]">
                  {/* relative: the bbox overlay below is positioned against
                      this box, not the page, so its percentages line up with
                      the video regardless of layout. */}
                  <div className="relative overflow-hidden rounded-md bg-black">
                    {/* No <source> child: the blob: URL is assigned to .src
                        directly by the fetch effect above, once the clip has
                        been retrieved with our auth header attached. */}
                    <video
                      ref={videoRef}
                      controls
                      onLoadedMetadata={handleLoadedMetadata}
                      onTimeUpdate={handleTimeUpdate}
                      className="w-full rounded-md"
                      style={{ aspectRatio: '16 / 9' }}
                    />
                    {/* The one thing this whole feature is for: where, in this
                        recorded frame, the flagged vehicle actually was. Drawn
                        only while playback is within BBOX_TOLERANCE_MS of the
                        detection's own instant - a box that lingered longer
                        would claim to be tracking a vehicle this data cannot
                        actually track. */}
                    {activeDetection && hasBBox(activeDetection) && (
                      <div
                        className="pointer-events-none absolute border-2 border-state-warn shadow-[0_0_0_1px_rgba(0,0,0,0.6)]"
                        style={{
                          left: `${activeDetection.bbox_x_min * 100}%`,
                          top: `${activeDetection.bbox_y_min * 100}%`,
                          width: `${(activeDetection.bbox_x_max - activeDetection.bbox_x_min) * 100}%`,
                          height: `${(activeDetection.bbox_y_max - activeDetection.bbox_y_min) * 100}%`,
                        }}
                      >
                        <span className="absolute -top-5 left-0 whitespace-nowrap rounded bg-state-warn px-1 text-[10px] font-medium text-ink-950">
                          {activeDetection.plate_number || 'vehicle'}
                        </span>
                      </div>
                    )}
                  </div>

                  {/* Companion evidence: the ANPR plate crop for whichever
                      detection the playhead is currently near. Absent
                      whenever nothing is that close - most of any clip, since
                      a detection is an instant, not a span. */}
                  {activeDetection && (
                    <div className="w-full space-y-1.5 lg:w-40">
                      <p className="text-[11px] font-medium text-slate-400">Captured frame</p>
                      <PlateSnapshot
                        eventId={activeDetection.event_id}
                        plateNumber={activeDetection.plate_number}
                        className="w-full"
                      />
                      <p className="font-mono text-xs text-accent">
                        {activeDetection.plate_number || '—'}
                      </p>
                      {activeDetection.plate_confidence != null && (
                        <Badge tone={activeDetection.plate_confidence >= 0.8 ? 'success' : 'warn'}>
                          {Math.round(activeDetection.plate_confidence * 100)}% read
                        </Badge>
                      )}
                    </div>
                  )}
                </div>
                {clipError && (
                  <p className="mt-2 text-xs text-state-down">Clip failed to load: {clipError}</p>
                )}
                {missedDeepLink && (
                  // A real, specific gap: the moment this clip was opened for
                  // is not on disk, most likely a brief drop in the camera's
                  // own connection (this grid does that) or a moment before
                  // recording began. The segment now showing is the nearest
                  // one available, not the one that was asked for - say so
                  // rather than let it pass as a match.
                  <p className="mt-2 text-xs text-state-warn">
                    No recording covers the exact moment this clip was opened for - showing the
                    nearest available segment instead. This usually means the camera's connection
                    dropped briefly around that time.
                  </p>
                )}
                {!selected && !recordings.isLoading && (
                  <p className="mt-2 text-xs text-slate-500">
                    No recorded segment selected for this window yet.
                  </p>
                )}
              </CardBody>
            </Card>

            <Card>
              <CardBody className="space-y-3">
                {recordings.isLoading || detections.isLoading ? (
                  <div className="flex justify-center p-6">
                    <Spinner className="h-5 w-5" />
                  </div>
                ) : recordings.isError ? (
                  <ErrorState error={recordings.error} onRetry={recordings.refetch} />
                ) : (
                  <>
                    {recordings.data?.note && (
                      <p className="text-[11px] text-state-warn">{recordings.data.note}</p>
                    )}
                    <Timeline
                      windowStart={windowStart}
                      windowEnd={windowEnd}
                      segments={segments}
                      detections={plates}
                      selectedStart={selected?.start}
                      onPick={setSelected}
                      onPickDetection={seekToDetection}
                    />
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="flex items-center gap-1.5">
                        <Button size="sm" variant="ghost" onClick={() => jumpToDetection(-1)}>
                          <SkipBack className="mr-1 h-3.5 w-3.5" aria-hidden />
                          Previous detection
                        </Button>
                        <Button size="sm" variant="ghost" onClick={() => jumpToDetection(1)}>
                          Next detection
                          <SkipForward className="ml-1 h-3.5 w-3.5" aria-hidden />
                        </Button>
                      </div>
                      <div className="flex items-center gap-1.5">
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => setWindowEnd(new Date(windowStart.getTime()))}
                        >
                          <ChevronLeft className="mr-1 h-3.5 w-3.5" aria-hidden />
                          Earlier
                        </Button>
                        <Button
                          size="sm"
                          variant="secondary"
                          onClick={() => setWindowEnd(new Date(windowEnd.getTime() + WINDOW_MS))}
                        >
                          Later
                          <ChevronRight className="ml-1 h-3.5 w-3.5" aria-hidden />
                        </Button>
                      </div>
                    </div>
                  </>
                )}
              </CardBody>
            </Card>
          </>
        )}
      </div>
    </div>
  );
}
