import React from 'react';

import PhasePlaceholder from './PhasePlaceholder.jsx';

export default function FederationPage() {
  return (
    <PhasePlaceholder
      title="VMS federation"
      model="Model 3"
      description="Middleware federating multiple departmental VMS platforms"
      planned={[
        'Connected platforms with per-connector health, camera counts and last sync',
        'Connector configuration: add a vendor VMS without touching code',
        'Cross-system event correlation — the same physical event reported by two platforms, deduplicated',
        'Unified workflow queue spanning every federated system',
      ]}
      dependencies={[
        'A connector framework with a BaseVMSConnector interface and concrete vendor adapters',
        'Two mock departmental VMS servers with deliberately different API shapes, since there is nothing to federate on a development machine',
        'A metadata exchange bus: new Kafka topics for camera, event and health messages, plus a normaliser',
        'Real vendor endpoints and credentials for a production demonstration',
      ]}
      available={[
        {
          label: 'Kafka message bus',
          note: 'running, and already carrying surveillance events — the federation topics would sit alongside them',
        },
        {
          label: 'External state-database adapters',
          note: 'adapters/ federates eGujCop and VAHAN, which is a different kind of federation from this screen (state registries, not VMS platforms)',
        },
      ]}
    />
  );
}
