import React, { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Camera, History, Pencil, Save, ShieldAlert, Trash2, X } from 'lucide-react';

import { PageHeader } from '../components/layout/AppShell.jsx';
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorState,
  Field,
  Input,
  Select,
  Spinner,
  StatusDot,
  Table,
  Td,
  Th,
  Textarea,
} from '../components/ui/index.jsx';
import FlagSuspiciousDialog from '../components/FlagSuspiciousDialog.jsx';
import { api } from '../lib/api.js';
import { useAuth } from '../lib/auth.jsx';

function formatTime(ms) {
  return new Date(ms).toLocaleString();
}

/**
 * Recent plate reads at this camera, so an operator who noticed something on
 * the video tile - not from a plate list - can still find what ANPR already
 * captured around that time and flag it, without knowing the plate in advance.
 */
function RecentPlateReads({ cameraId, onFlag }) {
  const reads = useQuery({
    queryKey: ['detections', 'by-camera', cameraId],
    queryFn: () => api.get('/api/v1/detections', { camera_id: cameraId, plates_only: true, limit: 20 }),
    refetchInterval: 15_000,
  });

  return (
    <Card>
      <CardHeader
        title="Recent plate reads"
        description="Newest first — refreshes automatically"
      />
      {reads.isLoading ? (
        <CardBody>
          <Spinner />
        </CardBody>
      ) : reads.isError ? (
        <CardBody>
          <ErrorState error={reads.error} onRetry={reads.refetch} />
        </CardBody>
      ) : reads.data?.items?.length ? (
        <Table>
          <thead>
            <tr>
              <Th>Plate</Th>
              <Th>Confidence</Th>
              <Th>Seen</Th>
              <Th className="text-right">Actions</Th>
            </tr>
          </thead>
          <tbody>
            {reads.data.items.map((item) => (
              <tr key={item.event_id}>
                <Td className="font-mono text-xs text-accent">{item.plate_number}</Td>
                <Td className="text-xs tabular-nums">
                  {item.plate_confidence != null ? `${Math.round(item.plate_confidence * 100)}%` : '—'}
                </Td>
                <Td className="whitespace-nowrap text-xs text-slate-400">
                  {formatTime(item.timestamp_utc_ms)}
                </Td>
                <Td className="text-right">
                  <Button
                    size="sm"
                    variant="ghost"
                    title="Flag as suspicious"
                    onClick={() => onFlag(item.plate_number)}
                  >
                    <ShieldAlert className="h-3.5 w-3.5" />
                  </Button>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      ) : (
        <CardBody>
          <EmptyState
            icon={Camera}
            title="No plate reads yet"
            description="ANPR reads from this camera appear here within seconds of being published."
          />
        </CardBody>
      )}
    </Card>
  );
}

const CAMERA_TYPES = ['FIXED', 'PTZ', 'DOME', 'BULLET', 'ANPR', 'THERMAL', 'PANORAMIC', 'OTHER'];
const STATUSES = ['ACTIVE', 'INACTIVE', 'MAINTENANCE', 'DECOMMISSIONED'];
const MAINTENANCE = ['OK', 'DUE', 'OVERDUE', 'IN_PROGRESS', 'FAULTY'];
const CONNECTIVITY_TYPES = ['FIBRE', 'ETHERNET', 'WIFI', 'CELLULAR_4G', 'CELLULAR_5G', 'RF', 'OTHER'];

/** Read-only definition row. */
function Detail({ label, value, mono }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-ink-800 py-1.5 last:border-0">
      <dt className="shrink-0 text-xs text-slate-500">{label}</dt>
      <dd
        className={`min-w-0 truncate text-right text-xs ${mono ? 'font-mono' : ''} ${
          value ? 'text-slate-200' : 'text-slate-600'
        }`}
      >
        {value || 'not recorded'}
      </dd>
    </div>
  );
}

export default function CameraDetailPage() {
  const { cameraId } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { atLeast } = useAuth();

  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState({});
  const [saveError, setSaveError] = useState(null);
  const [flagPlate, setFlagPlate] = useState(null);

  const camera = useQuery({
    queryKey: ['camera', cameraId],
    queryFn: () => api.get(`/api/v1/cameras/${cameraId}`),
  });

  const health = useQuery({
    queryKey: ['camera-health', cameraId],
    queryFn: () => api.get(`/api/v1/cameras/${cameraId}/health`),
    refetchInterval: 30_000,
  });

  const history = useQuery({
    queryKey: ['audit', 'cameras', cameraId],
    queryFn: () =>
      api.get('/api/v1/audit', { entity: 'cameras', entity_id: cameraId, limit: 20 }),
    enabled: atLeast('DEPT_ADMIN'),
  });

  // Seed the edit form from the loaded row rather than from a blank object, so
  // an untouched field submits its current value and nothing is cleared by
  // accident.
  useEffect(() => {
    if (camera.data && !editing) setForm(camera.data);
  }, [camera.data, editing]);

  const save = useMutation({
    mutationFn: (changes) => api.patch(`/api/v1/cameras/${cameraId}`, changes),
    onSuccess: (updated) => {
      queryClient.setQueryData(['camera', cameraId], updated);
      queryClient.invalidateQueries({ queryKey: ['cameras'] });
      queryClient.invalidateQueries({ queryKey: ['audit'] });
      setEditing(false);
      setSaveError(null);
    },
    onError: (error) => setSaveError(error),
  });

  const decommission = useMutation({
    mutationFn: () => api.delete(`/api/v1/cameras/${cameraId}`),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['cameras'] });
      navigate('/registry');
    },
  });

  if (camera.isLoading) {
    return (
      <div className="flex justify-center p-12">
        <Spinner className="h-6 w-6" />
      </div>
    );
  }
  if (camera.isError) {
    return (
      <div className="p-6">
        <ErrorState error={camera.error} onRetry={camera.refetch} />
      </div>
    );
  }

  const data = camera.data;

  function submit(event) {
    event.preventDefault();
    // Send only what actually changed: the API treats an omitted field as
    // "leave alone", which is what keeps a partial edit safe.
    const changes = {};
    for (const [key, value] of Object.entries(form)) {
      if (value !== data[key] && value !== '' && value !== null) changes[key] = value;
    }
    // Latitude and longitude must travel together or the API rejects the pair.
    if ('latitude' in changes || 'longitude' in changes) {
      changes.latitude = Number(form.latitude);
      changes.longitude = Number(form.longitude);
    }
    for (const numeric of ['azimuth_angle', 'fov_degrees', 'frame_rate', 'retention_days']) {
      if (numeric in changes) changes[numeric] = Number(changes[numeric]);
    }
    if (Object.keys(changes).length === 0) {
      setEditing(false);
      return;
    }
    save.mutate(changes);
  }

  const set = (key) => (event) => setForm((f) => ({ ...f, [key]: event.target.value }));

  return (
    <div className="min-h-full">
      <PageHeader
        title={data.global_camera_code}
        description={data.site_name || 'Site name not surveyed'}
        actions={
          <>
            <Link to="/registry">
              <Button variant="ghost" size="sm">
                <ArrowLeft className="h-3.5 w-3.5" /> Registry
              </Button>
            </Link>
            <Link to={`/map?camera=${data.id}`}>
              <Button variant="secondary" size="sm">
                View on map
              </Button>
            </Link>
            {atLeast('DEPT_ADMIN') &&
              (editing ? (
                <Button variant="ghost" size="sm" onClick={() => setEditing(false)}>
                  <X className="h-3.5 w-3.5" /> Cancel
                </Button>
              ) : (
                <Button variant="primary" size="sm" onClick={() => setEditing(true)}>
                  <Pencil className="h-3.5 w-3.5" /> Edit
                </Button>
              ))}
          </>
        }
      />

      <div className="grid gap-4 p-4 sm:p-6 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          {editing ? (
            <Card>
              <CardHeader
                title="Edit camera metadata"
                description="Only changed fields are submitted"
              />
              <form onSubmit={submit}>
                <CardBody className="grid gap-3 sm:grid-cols-2">
                  <Field label="Site name">
                    <Input value={form.site_name || ''} onChange={set('site_name')} />
                  </Field>
                  <Field label="Camera type">
                    <Select value={form.camera_type || ''} onChange={set('camera_type')}>
                      <option value="">Not recorded</option>
                      {CAMERA_TYPES.map((t) => (
                        <option key={t}>{t}</option>
                      ))}
                    </Select>
                  </Field>
                  <Field label="Make">
                    <Input value={form.make || ''} onChange={set('make')} />
                  </Field>
                  <Field label="Model">
                    <Input value={form.model || ''} onChange={set('model')} />
                  </Field>
                  <Field label="Latitude" hint="WGS84 decimal degrees">
                    <Input value={form.latitude ?? ''} onChange={set('latitude')} />
                  </Field>
                  <Field label="Longitude">
                    <Input value={form.longitude ?? ''} onChange={set('longitude')} />
                  </Field>
                  <Field label="Azimuth" hint="0 = true north, clockwise">
                    <Input value={form.azimuth_angle ?? ''} onChange={set('azimuth_angle')} />
                  </Field>
                  <Field label="Field of view (degrees)">
                    <Input value={form.fov_degrees ?? ''} onChange={set('fov_degrees')} />
                  </Field>
                  <Field label="Installed on" hint="Physical installation, not registration">
                    <Input
                      type="date"
                      value={form.installed_on || ''}
                      onChange={set('installed_on')}
                    />
                  </Field>
                  <Field label="Lifecycle status">
                    <Select value={form.status || ''} onChange={set('status')}>
                      {STATUSES.map((s) => (
                        <option key={s}>{s}</option>
                      ))}
                    </Select>
                  </Field>
                  <Field label="Maintenance state">
                    <Select value={form.maintenance_state || ''} onChange={set('maintenance_state')}>
                      <option value="">Not recorded</option>
                      {MAINTENANCE.map((m) => (
                        <option key={m}>{m}</option>
                      ))}
                    </Select>
                  </Field>
                  <Field label="Next service due">
                    <Input
                      type="date"
                      value={form.next_service_due || ''}
                      onChange={set('next_service_due')}
                    />
                  </Field>
                  <Field label="Connectivity type">
                    <Select value={form.connectivity_type || ''} onChange={set('connectivity_type')}>
                      <option value="">Not recorded</option>
                      {CONNECTIVITY_TYPES.map((c) => (
                        <option key={c}>{c}</option>
                      ))}
                    </Select>
                  </Field>
                  <Field label="Resolution">
                    <Input
                      value={form.resolution || ''}
                      onChange={set('resolution')}
                      placeholder="1920x1080"
                    />
                  </Field>
                  <Field label="Ward">
                    <Input value={form.ward || ''} onChange={set('ward')} />
                  </Field>
                  <Field label="Owner organisation">
                    <Input value={form.owner_org || ''} onChange={set('owner_org')} />
                  </Field>
                  <Field label="Retention (days)">
                    <Input value={form.retention_days ?? ''} onChange={set('retention_days')} />
                  </Field>
                  <Field label="NVR reference">
                    <Input value={form.nvr_reference || ''} onChange={set('nvr_reference')} />
                  </Field>
                  <Field label="Notes" className="sm:col-span-2">
                    <Textarea rows={2} value={form.notes || ''} onChange={set('notes')} />
                  </Field>

                  {saveError && (
                    <div className="sm:col-span-2">
                      <ErrorState error={saveError} />
                    </div>
                  )}
                </CardBody>
                <div className="flex justify-end gap-2 border-t border-ink-700 px-4 py-3">
                  <Button variant="ghost" onClick={() => setEditing(false)}>
                    Cancel
                  </Button>
                  <Button type="submit" variant="primary" disabled={save.isPending}>
                    {save.isPending ? <Spinner className="h-3.5 w-3.5" /> : <Save className="h-3.5 w-3.5" />}
                    Save changes
                  </Button>
                </div>
              </form>
            </Card>
          ) : (
            <div className="grid gap-4 sm:grid-cols-2">
              <Card>
                <CardHeader title="Identity & ownership" />
                <CardBody>
                  <dl>
                    <Detail label="Camera code" value={data.global_camera_code} mono />
                    <Detail label="Department" value={data.department_id} />
                    <Detail label="Owner" value={data.owner_org} />
                    <Detail label="Custodian" value={data.custodian_name} />
                    <Detail label="Contact" value={data.custodian_contact} />
                    <Detail label="VMS vendor" value={data.vms_vendor} />
                  </dl>
                </CardBody>
              </Card>

              <Card>
                <CardHeader title="Hardware" />
                <CardBody>
                  <dl>
                    <Detail label="Type" value={data.camera_type} />
                    <Detail label="Make" value={data.make} />
                    <Detail label="Model" value={data.model} />
                    <Detail label="Serial" value={data.serial_number} mono />
                    <Detail label="Resolution" value={data.resolution} />
                    <Detail label="Codec" value={data.codec} />
                    <Detail label="Frame rate" value={data.frame_rate && `${data.frame_rate} fps`} />
                    <Detail label="Installed" value={data.installed_on} />
                  </dl>
                </CardBody>
              </Card>

              <Card>
                <CardHeader title="Location" />
                <CardBody>
                  <dl>
                    <Detail label="Site" value={data.site_name} />
                    <Detail label="Address" value={data.address} />
                    <Detail label="Ward" value={data.ward} />
                    <Detail label="Zone" value={data.zone} />
                    <Detail label="District" value={data.district} />
                    <Detail
                      label="Coordinates"
                      value={`${data.latitude.toFixed(5)}, ${data.longitude.toFixed(5)}`}
                      mono
                    />
                    <Detail
                      label="Azimuth / FOV"
                      value={`${data.azimuth_angle ?? '—'}° / ${data.fov_degrees ?? '—'}°`}
                    />
                  </dl>
                </CardBody>
              </Card>

              <Card>
                <CardHeader title="Network & storage" />
                <CardBody>
                  <dl>
                    <Detail label="IP address" value={data.ip_address} mono />
                    <Detail label="Link type" value={data.connectivity_type} />
                    <Detail label="Stream URL" value={data.stream_url} mono />
                    <Detail label="NVR" value={data.nvr_reference} />
                    <Detail label="NVR channel" value={data.nvr_channel} />
                    <Detail
                      label="Retention"
                      value={data.retention_days != null && `${data.retention_days} days`}
                    />
                    <Detail label="Recording" value={data.recording_enabled ? 'Enabled' : 'Disabled'} />
                  </dl>
                </CardBody>
              </Card>
            </div>
          )}

          <RecentPlateReads cameraId={data.id} onFlag={setFlagPlate} />

          {atLeast('DEPT_ADMIN') && (
            <Card>
              <CardHeader
                title="Change history"
                description="Written by a database trigger, so no edit can bypass it"
                actions={<History className="h-4 w-4 text-slate-500" />}
              />
              {history.isLoading ? (
                <CardBody>
                  <Spinner />
                </CardBody>
              ) : history.data?.items?.length ? (
                <Table>
                  <thead>
                    <tr>
                      <Th>When</Th>
                      <Th>Action</Th>
                      <Th>Changed</Th>
                      <Th>By</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.data.items.map((entry) => (
                      <tr key={entry.id}>
                        <Td className="whitespace-nowrap text-xs tabular-nums text-slate-400">
                          {new Date(entry.at).toLocaleString()}
                        </Td>
                        <Td>
                          <Badge tone={entry.action === 'INSERT' ? 'success' : 'info'}>
                            {entry.action}
                          </Badge>
                        </Td>
                        <Td className="text-xs">
                          {entry.changed
                            .filter((field) => field !== 'updated_at')
                            .join(', ') || '—'}
                        </Td>
                        <Td className="text-xs text-slate-400">
                          {entry.actor_label || 'system'}
                        </Td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
              ) : (
                <CardBody>
                  <p className="text-xs text-slate-500">No recorded changes yet.</p>
                </CardBody>
              )}
            </Card>
          )}
        </div>

        <div className="space-y-4">
          <Card>
            <CardHeader title="Live status" />
            <CardBody className="space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-xs text-slate-500">Lifecycle</span>
                <Badge tone={data.status === 'ACTIVE' ? 'success' : 'warn'}>{data.status}</Badge>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-xs text-slate-500">Reachability</span>
                <span className="inline-flex items-center gap-1.5 text-xs text-slate-200">
                  <StatusDot state={health.data?.status} />
                  {health.data?.status || '…'}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-xs text-slate-500">Latency</span>
                <span className="text-xs tabular-nums text-slate-200">
                  {health.data?.ping_latency_ms != null ? `${health.data.ping_latency_ms} ms` : '—'}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-xs text-slate-500">Last poll</span>
                <span className="text-xs text-slate-200">
                  {health.data?.last_ping_at
                    ? new Date(health.data.last_ping_at).toLocaleTimeString()
                    : 'never'}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-xs text-slate-500">Maintenance</span>
                <Badge tone={data.maintenance_state === 'OK' ? 'neutral' : 'warn'}>
                  {data.maintenance_state || 'OK'}
                </Badge>
              </div>
            </CardBody>
          </Card>

          {atLeast('DEPT_ADMIN') && data.status !== 'DECOMMISSIONED' && (
            <Card className="border-dept-police/30">
              <CardHeader title="Decommission" description="Marks the camera retired" />
              <CardBody className="space-y-3">
                <p className="text-xs text-slate-400">
                  The row is kept and marked DECOMMISSIONED, never deleted — detections, alerts
                  and sessions reference this camera, and the retirement is itself an auditable
                  fact.
                </p>
                <Button
                  variant="danger"
                  size="sm"
                  disabled={decommission.isPending}
                  onClick={() => {
                    if (
                      window.confirm(
                        `Decommission ${data.global_camera_code}? It will stop appearing as an active camera.`,
                      )
                    ) {
                      decommission.mutate();
                    }
                  }}
                >
                  <Trash2 className="h-3.5 w-3.5" /> Decommission camera
                </Button>
                {decommission.isError && <ErrorState error={decommission.error} />}
              </CardBody>
            </Card>
          )}
        </div>
      </div>

      {flagPlate && <FlagSuspiciousDialog plate={flagPlate} onClose={() => setFlagPlate(null)} />}
    </div>
  );
}
