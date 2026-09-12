import React from 'react';

import PhasePlaceholder from './PhasePlaceholder.jsx';

export default function VideoWallPage() {
  return (
    <PhasePlaceholder
      title="Video wall"
      model="Model 2"
      description="Multi-camera grid view across departmental systems"
      planned={[
        '1, 4, 9 and 16-tile layouts with per-tile camera selection',
        'Saved wall presets per operator, restored on sign-in',
        'Per-tile WebRTC lifecycle so one failing camera does not blank the wall',
        'A concurrent-stream cap, since every tile holds a media session',
        'Tile overlays: camera code, department, health dot, plate reads as they land',
      ]}
      dependencies={[
        'The single-stream WebRTC path already works; it needs generalising from one popup video element to N independent tiles',
        'A per-user preset store (table plus endpoints)',
        'Feeds from a second departmental system, to satisfy the tender requirement that the wall shows at least two different systems at once',
      ]}
      available={[
        {
          label: 'Single-camera live view',
          to: '/map',
          note: 'click any camera marker, then Request Live Stream',
        },
        {
          label: 'MediaMTX media plane',
          note: 'RTSP ingest and WebRTC delivery are running and verified',
        },
      ]}
    />
  );
}
