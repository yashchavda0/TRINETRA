import React from 'react';

import PhasePlaceholder from './PhasePlaceholder.jsx';

export default function VehicleSearchPage() {
  return (
    <PhasePlaceholder
      title="Vehicle search"
      model="Model 2"
      description="Searchable vehicle-movement records from ANPR metadata"
      planned={[
        'Search by plate (exact and fuzzy), camera, time range and object class',
        'Movement timeline for a plate: every sighting, in order, with inter-camera transit times',
        'Plot a vehicle route on the GIS map using the existing trajectory renderer',
        'Event tagging and a camera-wise index of what each camera saw',
      ]}
      dependencies={[
        'The ANPR service: sample frames from each live camera, read plates, publish them to the analytics bus',
        'Read endpoints over the detections table — it is written today and has no read API at all',
        'The plate column added in migration 003 needs populating by the ANPR path before search returns anything',
      ]}
      available={[
        {
          label: 'Detections storage and indexes',
          note: 'detections and threat_alerts are written by the handoff worker; the indexes for plate and time search already exist',
        },
        {
          label: 'Plate screening against eGujCop and VAHAN',
          note: 'runs in simulation mode until real registry credentials are supplied',
        },
      ]}
    />
  );
}
