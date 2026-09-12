/**
 * One camera on the video wall.
 *
 * Every failure mode gets a rendered state. A blank black rectangle is the one
 * outcome this component must never produce: on a wall of twelve, an operator
 * cannot tell an undecodable codec from a dead camera from a torn-down session
 * unless the tile says which it is.
 */

import React from 'react';
import { AlertTriangle, PauseCircle, RefreshCw, VideoOff } from 'lucide-react';

import { Button, Spinner, StatusDot, cn } from './ui/index.jsx';
import { useCameraStream } from '../hooks/useCameraStream.js';

const DEPARTMENT_DOT = {
  POLICE: 'bg-dept-police',
  RTO: 'bg-dept-rto',
  GSRTC: 'bg-dept-gsrtc',
  CIVIL_SUPPLIES: 'bg-dept-civil',
  REVENUE: 'bg-dept-revenue',
  PRIVATE: 'bg-dept-private',
};

export default function LiveTile({ camera, enabled = true, onOpen }) {
  const { videoRef, status, error, summary, start, paused } = useCameraStream({
    cameraId: camera.id,
    enabled,
    autoRetry: true,
    pauseWhenHidden: true,
  });

  const code = camera.global_camera_code || camera.id;

  return (
    <div className="group relative overflow-hidden rounded-lg border border-ink-700 bg-black">
      {/* aspect-video reserves the box before the first frame: a <video> with no
          stream has no intrinsic size, and a collapsed tile reads as a broken
          layout rather than one that is still connecting. */}
      <div className="relative aspect-video w-full">
        <video
          ref={videoRef}
          autoPlay
          playsInline
          muted
          className={cn(
            'h-full w-full object-contain',
            status === 'live' ? 'opacity-100' : 'opacity-0',
          )}
        />

        {status !== 'live' && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 px-3 text-center">
            {paused ? (
              <>
                <PauseCircle className="h-6 w-6 text-slate-600" aria-hidden />
                <p className="text-[11px] text-slate-500">paused — not being watched</p>
              </>
            ) : status === 'connecting' ? (
              <>
                <Spinner className="h-5 w-5" />
                <p className="text-[11px] text-slate-500">connecting…</p>
              </>
            ) : status === 'error' ? (
              <>
                <VideoOff className="h-6 w-6 text-state-down" aria-hidden />
                {/* The API's own sentence, e.g. "camera is INACTIVE, not ACTIVE". */}
                <p className="text-[11px] leading-snug text-slate-400">{error}</p>
                <Button size="sm" variant="ghost" onClick={start} className="mt-1">
                  <RefreshCw className="mr-1 h-3 w-3" aria-hidden />
                  Retry
                </Button>
              </>
            ) : (
              <p className="text-[11px] text-slate-600">idle</p>
            )}
          </div>
        )}

        {/* Bytes arriving with nothing decoded - the H.265 case. The stream is
            healthy, so this is a warning over the picture, not an error state. */}
        {status === 'live' && summary?.warn && (
          <div className="absolute inset-x-0 bottom-0 flex items-start gap-1.5 bg-ink-950/85 px-2 py-1.5">
            <AlertTriangle className="mt-px h-3 w-3 shrink-0 text-state-warn" aria-hidden />
            <p className="text-[11px] leading-snug text-state-warn">{summary.text}</p>
          </div>
        )}
      </div>

      {/* Identity strip. Always visible: on a wall, a picture nobody can place
          is not intelligence. */}
      <div className="flex items-center gap-2 border-t border-ink-700 bg-ink-850 px-2 py-1.5">
        <span
          className={cn(
            'inline-block h-2 w-2 shrink-0 rounded-full',
            DEPARTMENT_DOT[String(camera.department_id || '').toUpperCase()] || 'bg-dept-private',
          )}
          title={camera.department_id || 'unassigned'}
        />
        <button
          type="button"
          onClick={() => onOpen?.(camera)}
          className="truncate font-mono text-xs text-accent hover:underline"
          title={camera.site_name || code}
        >
          {code}
        </button>
        <span className="ml-auto flex items-center gap-1.5">
          {status === 'live' && !summary?.warn && (
            <span className="hidden text-[11px] text-slate-500 group-hover:inline">
              {summary?.text}
            </span>
          )}
          <StatusDot state={status === 'live' ? 'UP' : status === 'error' ? 'DOWN' : 'UNKNOWN'} />
        </span>
      </div>
    </div>
  );
}
