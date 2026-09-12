import React from 'react';

import GISMap from '../components/GISMap.jsx';
import { useAuth } from '../lib/auth.jsx';

// Ahmedabad, in the [lon, lat] order OpenLayers expects.
const DEFAULT_CENTER = [72.5714, 23.0225];

// The P0 alert channel authenticates with its own shared key, NOT the user's
// access token: app/routers/alerts.py compares the value literally against
// P0_ALERT_API_KEY and never decodes a JWT, so passing the session token closes
// the handshake with 1008 and the badge reconnect-loops. Keeping the JWT out of
// a query string is the second reason - URLs land in proxy and server logs.
const ALERT_TOKEN = import.meta.env.VITE_P0_ALERT_TOKEN || null;

/**
 * GIS operations map.
 *
 * GISMap already fetches through the shared API client, so the bearer token is
 * attached for it. The alert socket is the exception: a browser cannot set
 * headers on a WebSocket handshake, so its key has to ride the query string,
 * which is why it is passed explicitly here.
 */
export default function MapPage() {
  const { user } = useAuth();

  return (
    <div className="relative h-full w-full">
      <GISMap
        apiBaseUrl="/api/v1"
        webrtcBaseUrl="/api/v2"
        alertsWsUrl="/alerts/p0"
        alertsToken={ALERT_TOKEN}
        center={DEFAULT_CENTER}
        zoom={12}
        key={user?.id /* remount on user change so scoped data is refetched */}
      />
    </div>
  );
}
