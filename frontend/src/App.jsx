import React, { useCallback, useState } from 'react';

import GISMap from './components/GISMap.jsx';

// Ahmedabad. Note the [lon, lat] order GISMap expects, matching OpenLayers.
const DEFAULT_CENTER = [72.5714, 23.0225];

/**
 * Console shell.
 *
 * Every backend URL is relative so it rides the Vite dev proxy (see
 * vite.config.js) and, in production, whatever reverse proxy fronts the API.
 * In particular alertsWsUrl is a path rather than the component's hardcoded
 * ws://central-command default, which resolves nowhere from a browser.
 */
export default function App() {
  const [alertCount, setAlertCount] = useState(0);
  const [lastAlert, setLastAlert] = useState(null);

  const handleAlert = useCallback((alert) => {
    setAlertCount((count) => count + 1);
    setLastAlert(alert);
  }, []);

  return (
    <div style={styles.shell}>
      <header style={styles.header}>
        <div style={styles.brand}>
          <span style={styles.mark}>TRINETRA</span>
          <span style={styles.subtitle}>Gujarat Police · Integrated CCTV Console</span>
        </div>
        <div style={styles.status}>
          {lastAlert ? (
            <span style={styles.alertBadge}>
              {alertCount} alert{alertCount === 1 ? '' : 's'} · last{' '}
              {lastAlert.classification || 'UNCLASSIFIED'}
              {lastAlert.plate_number ? ` · ${lastAlert.plate_number}` : ''}
            </span>
          ) : (
            <span style={styles.idleBadge}>no alerts this session</span>
          )}
        </div>
      </header>

      <main style={styles.map}>
        <GISMap
          apiBaseUrl="/api/v1"
          webrtcBaseUrl="/api/v2"
          alertsWsUrl="/alerts/p0"
          center={DEFAULT_CENTER}
          zoom={11}
          onAlert={handleAlert}
        />
      </main>
    </div>
  );
}

const styles = {
  shell: {
    display: 'flex',
    flexDirection: 'column',
    height: '100%',
    width: '100%',
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 16,
    padding: '10px 16px',
    borderBottom: '1px solid #1f2a37',
    background: '#0f1620',
    flex: '0 0 auto',
  },
  brand: { display: 'flex', alignItems: 'baseline', gap: 12, minWidth: 0 },
  mark: { fontSize: 18, fontWeight: 700, letterSpacing: 1.5 },
  subtitle: {
    fontSize: 12,
    color: '#8b98a5',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
  },
  status: { flex: '0 0 auto' },
  alertBadge: {
    fontSize: 12,
    padding: '4px 10px',
    borderRadius: 999,
    background: '#3b0d0d',
    border: '1px solid #7f1d1d',
    color: '#fecaca',
  },
  idleBadge: {
    fontSize: 12,
    padding: '4px 10px',
    borderRadius: 999,
    background: '#111c26',
    border: '1px solid #1f2a37',
    color: '#8b98a5',
  },
  map: { flex: '1 1 auto', minHeight: 0, position: 'relative' },
};
