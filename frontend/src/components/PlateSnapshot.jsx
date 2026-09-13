import React from 'react';
import { ImageOff } from 'lucide-react';

import { Spinner } from './ui/index.jsx';
import { useAuthedImage } from '../hooks/useAuthedImage.js';

/**
 * The ANPR plate-crop image for one detection - what the OCR actually read,
 * not the full camera frame. Small by nature (these run 40-450px wide): this
 * is evidence for the plate text, not a scene view. For frame context, the
 * console overlays the same detection's bbox on the recorded clip instead -
 * see PlaybackPage.
 */
export default function PlateSnapshot({ eventId, plateNumber, className }) {
  const { url, error } = useAuthedImage(eventId ? `/api/v1/detections/${eventId}/snapshot` : null);

  if (!eventId) return null;

  if (error) {
    return (
      <div className={className}>
        <div className="flex items-center gap-1.5 text-[11px] text-slate-500">
          <ImageOff className="h-3.5 w-3.5" aria-hidden />
          no snapshot
        </div>
      </div>
    );
  }

  if (!url) {
    return (
      <div className={className}>
        <Spinner className="h-4 w-4" />
      </div>
    );
  }

  return (
    <div className={className}>
      <img
        src={url}
        alt={plateNumber ? `Plate crop: ${plateNumber}` : 'Plate crop'}
        className="rounded border border-ink-700 bg-black"
        style={{ imageRendering: 'pixelated' }}
      />
    </div>
  );
}
