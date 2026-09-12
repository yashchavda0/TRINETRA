import React from 'react';

import PhasePlaceholder from './PhasePlaceholder.jsx';

export default function PlaybackPage() {
  return (
    <PhasePlaceholder
      title="Recording & playback"
      model="Model 4"
      description="Central VMS recording, tiered storage and timeline playback"
      planned={[
        'Per-camera recording policy driven by the retention_days column',
        '24-hour timeline showing which segments exist, with scrub, speed control and frame step',
        'Clip export to an evidence store, referenced from the alert it supports',
        'Storage tiering: hot on local disk, warm and cold in object storage',
      ]}
      dependencies={[
        'MediaMTX recording enabled, plus a recordings table indexing every segment',
        'Object storage (MinIO) in the stack, and a tiering worker that moves and reindexes segments',
        'Substantial disk: recording an 80,000-camera fleet is a capacity-planning exercise before it is a software one',
      ]}
      available={[
        {
          label: 'Live streaming through the media plane',
          to: '/map',
          note: 'RTSP ingest and WebRTC delivery work today; recording is the piece not yet turned on',
        },
        {
          label: 'Retention policy per camera',
          to: '/registry',
          note: 'the retention_days and recording_enabled fields are captured in the registry already',
        },
      ]}
    />
  );
}
