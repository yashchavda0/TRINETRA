import React from 'react';

import PhasePlaceholder from './PhasePlaceholder.jsx';

export default function AlertsPage() {
  return (
    <PhasePlaceholder
      title="Alerts"
      model="Model 2"
      description="Threat alert inbox, acknowledgement and watchlist management"
      planned={[
        'Alert inbox with filters by priority, classification, camera and time',
        'Acknowledge, assign and resolve, using the workflow columns added in migration 003',
        'Watchlist management for vehicles and targets of interest',
        'Click an alert to jump to its camera on the map with the track plotted',
      ]}
      dependencies={[
        'A read endpoint over threat_alerts: alerts are written and fanned out live, but never read back, so an operator who connects a second late cannot recover them',
        'Watchlist CRUD endpoints, plus matching in the worker',
      ]}
      available={[
        {
          label: 'Live P0 alert stream on the map',
          to: '/map',
          note: 'the HUD shows the channel state and plots each alert as it arrives',
        },
        {
          label: 'Alert publication and audit',
          note: 'every alert is written to threat_alerts before fan-out, so the record exists — only the read path is missing',
        },
      ]}
    />
  );
}
