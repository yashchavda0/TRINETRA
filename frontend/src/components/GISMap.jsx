/**
 * TRINETRA GIS operations map.
 *
 * Rendering contract: the OpenLayers canvas is created exactly once and lives in
 * refs. Incoming WebSocket alerts mutate the vector sources directly, so a
 * thousand alerts per minute never re-mount the map. React state is used only
 * for the chrome that genuinely has to re-render (popup, stream modal, counters).
 *
 * Coordinates arrive as WGS84 / EPSG:4326 (latitude, longitude) and are
 * projected to EPSG:3857 at the single `fromLonLat` boundary below.
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import Map from 'ol/Map';
import View from 'ol/View';
import Feature from 'ol/Feature';
import Overlay from 'ol/Overlay';
import TileLayer from 'ol/layer/Tile';
import VectorLayer from 'ol/layer/Vector';
import VectorSource from 'ol/source/Vector';
import OSM from 'ol/source/OSM';
import VectorTileLayer from 'ol/layer/VectorTile';
import VectorTileSource from 'ol/source/VectorTile';
import MVT from 'ol/format/MVT';
import Point from 'ol/geom/Point';
import LineString from 'ol/geom/LineString';
import { fromLonLat } from 'ol/proj';
import { Fill, Icon, Stroke, Style, Text, Circle as CircleStyle } from 'ol/style';
import { getVectorContext } from 'ol/render';
import { unByKey } from 'ol/Observable';

import 'ol/ol.css';

// Every call goes through the shared client so the bearer token is attached in
// one place. Calling fetch() directly here would 401 against the authenticated
// registry - which surfaces as "registry unreachable" and an empty map, with
// nothing to say the session was the problem.
import { api, request } from '../lib/api.js';

/* ------------------------------------------------------------------ */
/* Department styling                                                  */
/* ------------------------------------------------------------------ */

const DEPARTMENT_COLOURS = {
  POLICE: '#e02020',
  RTO: '#1d6fe0',
  CIVIL_SUPPLIES: '#17a94b',
  GSRTC: '#f0a020',
  REVENUE: '#8a3ffc',
  PRIVATE: '#6b7280',
  DEFAULT: '#6b7280',
};

const HEALTH_COLOURS = {
  ACTIVE: '#17a94b',
  DEGRADED: '#f0a020',
  MAINTENANCE: '#f0a020',
  INACTIVE: '#9ca3af',
  OFFLINE: '#e02020',
  DECOMMISSIONED: '#4b5563',
};

const departmentColour = (departmentId) =>
  DEPARTMENT_COLOURS[String(departmentId || '').toUpperCase()] || DEPARTMENT_COLOURS.DEFAULT;

/** Camera-body SVG rendered as a data URI so no marker assets have to ship. */
const cameraSvg = (colour, offline) => {
  const body = offline ? '#9ca3af' : colour;
  const svg = `
<svg xmlns="http://www.w3.org/2000/svg" width="34" height="34" viewBox="0 0 34 34">
  <circle cx="17" cy="17" r="15" fill="${body}" fill-opacity="0.18" stroke="${body}" stroke-width="2"/>
  <path d="M9 13.5h11.5a1.5 1.5 0 0 1 1.5 1.5v4a1.5 1.5 0 0 1-1.5 1.5H9a1.5 1.5 0 0 1-1.5-1.5v-4A1.5 1.5 0 0 1 9 13.5z" fill="${body}"/>
  <path d="M22 16l4.5-2.6v7.2L22 18z" fill="${body}"/>
  <circle cx="12.5" cy="17" r="1.8" fill="#ffffff"/>
</svg>`.trim();
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
};

// `Map` is shadowed by ol/Map in this module, so the native constructor is
// reached through globalThis.
const styleCache = new globalThis.Map();

const cameraStyle = (feature) => {
  const department = String(feature.get('department_id') || 'DEFAULT').toUpperCase();
  const status = String(feature.get('status') || 'ACTIVE').toUpperCase();
  const selected = Boolean(feature.get('__selected'));
  const key = `${department}|${status}|${selected}`;
  let style = styleCache.get(key);
  if (!style) {
    const offline = status === 'OFFLINE' || status === 'INACTIVE' || status === 'DECOMMISSIONED';
    style = new Style({
      image: new Icon({
        src: cameraSvg(departmentColour(department), offline),
        scale: selected ? 1.35 : 1,
        anchor: [0.5, 0.5],
      }),
    });
    styleCache.set(key, style);
  }
  return style;
};

/* ------------------------------------------------------------------ */
/* Trajectory styling                                                  */
/* ------------------------------------------------------------------ */

const TRAJECTORY_GLOW = new Style({
  stroke: new Stroke({ color: 'rgba(224, 32, 32, 0.25)', width: 10 }),
});

const trajectoryDashStyle = (offset) =>
  new Style({
    stroke: new Stroke({
      color: '#e02020',
      width: 3,
      lineDash: [12, 10],
      lineDashOffset: offset,
    }),
  });

const hitStyle = (index, total) =>
  new Style({
    image: new CircleStyle({
      radius: index === total - 1 ? 8 : 5,
      fill: new Fill({ color: index === total - 1 ? '#e02020' : '#ffffff' }),
      stroke: new Stroke({ color: '#e02020', width: 2 }),
    }),
    text: new Text({
      text: String(index + 1),
      offsetY: -14,
      font: '600 11px system-ui, sans-serif',
      fill: new Fill({ color: '#111827' }),
      stroke: new Stroke({ color: '#ffffff', width: 3 }),
    }),
  });

/* ------------------------------------------------------------------ */
/* Component                                                           */
/* ------------------------------------------------------------------ */

export default function GISMap({
  apiBaseUrl = '/api/v1',
  webrtcBaseUrl = '/api/v2',
  alertsWsUrl = 'ws://central-command/alerts/p0',
  // P0_ALERT_API_KEY. The API rejects the handshake with a policy-violation
  // close (the browser reports it as 403) when a key is configured server-side
  // and the socket presents none. A browser cannot set headers on a WebSocket,
  // so the key has to ride the query string - which is why the API accepts
  // ?token= as well as X-API-Key.
  alertsToken = null,
  center = [72.5714, 23.0225], // [lon, lat] — Ahmedabad
  zoom = 11,
  vectorTileUrl = null,
  maxTrajectoryPoints = 50,
  onAlert = null,
}) {
  const mapContainerRef = useRef(null);
  const popupContainerRef = useRef(null);

  const mapRef = useRef(null);
  const cameraSourceRef = useRef(null);
  const trajectorySourceRef = useRef(null);
  const trajectoryLayerRef = useRef(null);
  const overlayRef = useRef(null);
  const selectedFeatureRef = useRef(null);
  const cameraIndexRef = useRef(new globalThis.Map()); // camera_id -> Feature
  const trajectoryRef = useRef([]); // chronological hits
  const socketRef = useRef(null);
  const reconnectRef = useRef(null);
  const onAlertRef = useRef(onAlert);
  // Lets the map click handler tear a stream down without taking closeStream as
  // a dependency, which would re-initialise the whole map on every render.
  const closeStreamRef = useRef(null);

  const [selectedCamera, setSelectedCamera] = useState(null);
  const [health, setHealth] = useState(null);
  const [streamState, setStreamState] = useState({ status: 'idle', error: null });
  // The MediaStream carrying the live tracks, held in state so the <video> can
  // be attached by an effect once it exists. Assigning srcObject inside the
  // ontrack handler instead would depend on the element already being mounted.
  const [mediaStream, setMediaStream] = useState(null);
  // What is actually arriving: MediaMTX's codec list plus the browser's own
  // decode statistics. A codec the browser cannot decode looks exactly like a
  // dead camera - both are a black tile - so this is the line that tells them
  // apart.
  const [mediaInfo, setMediaInfo] = useState(null);
  const [alertCount, setAlertCount] = useState(0);
  const [latestAlert, setLatestAlert] = useState(null);
  const [socketStatus, setSocketStatus] = useState('connecting');
  // Registry load state is surfaced in the HUD rather than only logged: an
  // empty map otherwise looks identical whether the fetch failed, the registry
  // is empty, or every row was rejected for want of coordinates.
  const [registryState, setRegistryState] = useState({
    status: 'loading',
    drawn: 0,
    received: 0,
    error: null,
  });

  useEffect(() => {
    onAlertRef.current = onAlert;
  }, [onAlert]);

  /* ---------------- map bootstrap (runs once) ---------------- */

  useEffect(() => {
    const cameraSource = new VectorSource({ wrapX: false });
    const trajectorySource = new VectorSource({ wrapX: false });
    cameraSourceRef.current = cameraSource;
    trajectorySourceRef.current = trajectorySource;

    const baseLayer = vectorTileUrl
      ? new VectorTileLayer({
          declutter: true,
          source: new VectorTileSource({ format: new MVT(), url: vectorTileUrl, maxZoom: 18 }),
        })
      : new TileLayer({ source: new OSM() });

    const trajectoryLayer = new VectorLayer({
      source: trajectorySource,
      // Static part of the style; the animated dash is drawn in `postrender`.
      style: (feature) =>
        feature.getGeometry().getType() === 'LineString'
          ? TRAJECTORY_GLOW
          : hitStyle(feature.get('seq'), feature.get('total')),
      zIndex: 5,
    });
    trajectoryLayerRef.current = trajectoryLayer;

    const cameraLayer = new VectorLayer({
      source: cameraSource,
      style: cameraStyle,
      zIndex: 10,
    });

    const overlay = new Overlay({
      element: popupContainerRef.current,
      autoPan: { animation: { duration: 200 } },
      positioning: 'bottom-center',
      offset: [0, -22],
    });
    overlayRef.current = overlay;

    const map = new Map({
      target: mapContainerRef.current,
      layers: [baseLayer, trajectoryLayer, cameraLayer],
      overlays: [overlay],
      view: new View({ center: fromLonLat(center), zoom, maxZoom: 20 }),
    });
    mapRef.current = map;

    // Marching-ants animation: redraw the dashed line with a moving offset.
    // Done on the render frame so React never participates in the animation.
    const animationKey = trajectoryLayer.on('postrender', (event) => {
      const line = trajectorySource
        .getFeatures()
        .find((f) => f.getGeometry().getType() === 'LineString');
      if (!line) return;
      const context = getVectorContext(event);
      context.setStyle(trajectoryDashStyle(-((event.frameState.time / 40) % 22)));
      context.drawGeometry(line.getGeometry());
      map.render();
    });

    const clickKey = map.on('click', (event) => {
      const feature = map.forEachFeatureAtPixel(event.pixel, (f, layer) =>
        layer === cameraLayer ? f : undefined,
      );
      if (!feature) {
        overlay.setPosition(undefined);
        // Tear the stream down, don't just hide it: closing the popup while a
        // peer connection is open leaks it and leaves the server session alive.
        closeStreamRef.current?.();
        setSelectedCamera(null);
        return;
      }
      if (selectedFeatureRef.current && selectedFeatureRef.current !== feature) {
        selectedFeatureRef.current.set('__selected', false);
      }
      feature.set('__selected', true);
      selectedFeatureRef.current = feature;
      overlay.setPosition(feature.getGeometry().getCoordinates());
      // Switching cameras must close the previous peer connection, not merely
      // reset the UI state, or the old session keeps running server-side.
      closeStreamRef.current?.();
      setSelectedCamera({
        id: feature.getId(),
        global_camera_code: feature.get('global_camera_code'),
        department_id: feature.get('department_id'),
        status: feature.get('status'),
        latitude: feature.get('latitude'),
        longitude: feature.get('longitude'),
        azimuth_angle: feature.get('azimuth_angle'),
        fov_degrees: feature.get('fov_degrees'),
        vms_vendor: feature.get('vms_vendor'),
      });
    });

    const pointerKey = map.on('pointermove', (event) => {
      if (event.dragging) return;
      const hit = map.hasFeatureAtPixel(event.pixel, { layerFilter: (l) => l === cameraLayer });
      map.getTargetElement().style.cursor = hit ? 'pointer' : '';
    });

    return () => {
      unByKey(animationKey);
      unByKey(clickKey);
      unByKey(pointerKey);
      map.setTarget(undefined);
      map.dispose();
      mapRef.current = null;
    };
    // Intentionally empty: the map instance must outlive prop changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* ---------------- camera registry load ---------------- */

  useEffect(() => {
    const controller = new AbortController();

    (async () => {
      try {
        const body = await request(`${apiBaseUrl}/cameras`, {
          method: 'GET',
          params: { limit: 10000 },
          signal: controller.signal,
        });
        const cameras = Array.isArray(body) ? body : body.items || [];
        const source = cameraSourceRef.current;
        if (!source) return;

        const features = cameras
          .filter((c) => Number.isFinite(c.latitude) && Number.isFinite(c.longitude))
          .map((camera) => {
            const feature = new Feature({
              geometry: new Point(fromLonLat([camera.longitude, camera.latitude])),
              ...camera,
            });
            feature.setId(camera.id ?? camera.global_camera_code);
            cameraIndexRef.current.set(String(feature.getId()), feature);
            if (camera.global_camera_code) {
              cameraIndexRef.current.set(String(camera.global_camera_code), feature);
            }
            return feature;
          });

        source.clear();
        source.addFeatures(features);
        setRegistryState({
          status: 'loaded',
          drawn: features.length,
          received: cameras.length,
          error: null,
        });
      } catch (error) {
        if (error.name !== 'AbortError') {
          console.error('Camera registry load failed', error);
          setRegistryState({
            // "unreachable" and "not signed in" look identical on an empty map,
            // and the fix is completely different, so they are named apart.
            status: error.status === 401 || error.status === 403 ? 'unauthorised' : 'error',
            drawn: 0,
            received: 0,
            error: error.detail || error.message,
          });
        }
      }
    })();

    return () => controller.abort();
  }, [apiBaseUrl]);

  /* ---------------- trajectory maintenance ---------------- */

  const appendTrajectoryHit = useCallback(
    (hit) => {
      const source = trajectorySourceRef.current;
      if (!source || !Number.isFinite(hit.longitude) || !Number.isFinite(hit.latitude)) return;

      const hits = [...trajectoryRef.current, hit]
        .sort((a, b) => a.detected_at - b.detected_at)
        .slice(-maxTrajectoryPoints);
      trajectoryRef.current = hits;

      source.clear();
      const coordinates = hits.map((h) => fromLonLat([h.longitude, h.latitude]));
      if (coordinates.length > 1) {
        source.addFeature(new Feature({ geometry: new LineString(coordinates) }));
      }
      coordinates.forEach((coordinate, index) => {
        const feature = new Feature({ geometry: new Point(coordinate) });
        feature.set('seq', index);
        feature.set('total', coordinates.length);
        feature.set('hit', hits[index]);
        source.addFeature(feature);
      });
    },
    [maxTrajectoryPoints],
  );

  /** Zoom to every registered camera - the answer to "where are my markers?". */
  const fitToCameras = useCallback(() => {
    const source = cameraSourceRef.current;
    const map = mapRef.current;
    if (!source || !map || source.getFeatures().length === 0) return;
    // maxZoom keeps a single camera from zooming to street level, which reads
    // as a broken map rather than a fitted one.
    map.getView().fit(source.getExtent(), {
      padding: [60, 60, 60, 60],
      maxZoom: 16,
      duration: 250,
    });
  }, []);

  const clearTrajectory = useCallback(() => {
    trajectoryRef.current = [];
    trajectorySourceRef.current?.clear();
  }, []);

  /* ---------------- P0 alert socket ---------------- */

  // Resolved against the page origin so a relative path works, then given the
  // token. Built here rather than in the caller so the key is appended exactly
  // once across reconnects.
  const alertsSocketUrl = useMemo(() => {
    const url = new URL(alertsWsUrl, window.location.href);
    if (url.protocol === 'http:') url.protocol = 'ws:';
    if (url.protocol === 'https:') url.protocol = 'wss:';
    if (alertsToken) url.searchParams.set('token', alertsToken);
    return url.toString();
  }, [alertsWsUrl, alertsToken]);

  useEffect(() => {
    let disposed = false;
    let attempt = 0;

    const connect = () => {
      if (disposed) return;
      let socket;
      try {
        socket = new WebSocket(alertsSocketUrl);
      } catch (error) {
        console.error('Alert socket construction failed', error);
        scheduleReconnect();
        return;
      }
      socketRef.current = socket;

      socket.onopen = () => {
        attempt = 0;
        setSocketStatus('live');
      };

      socket.onmessage = (event) => {
        let alert;
        try {
          alert = JSON.parse(event.data);
        } catch {
          console.warn('Discarded malformed alert frame');
          return;
        }
        if (!alert || typeof alert !== 'object') return;

        // Prefer registry coordinates; the alert's own lat/lon is the fallback.
        const cameraFeature = alert.camera_id
          ? cameraIndexRef.current.get(String(alert.camera_id))
          : undefined;
        const latitude = cameraFeature?.get('latitude') ?? alert.latitude;
        const longitude = cameraFeature?.get('longitude') ?? alert.longitude;

        appendTrajectoryHit({
          alert_id: alert.alert_id,
          camera_id: alert.camera_id,
          camera_code: cameraFeature?.get('global_camera_code') ?? alert.camera_id,
          plate_number: alert.plate_number,
          classification: alert.classification,
          detected_at: Number(alert.detected_at) || Date.now(),
          latitude,
          longitude,
        });

        setAlertCount((count) => count + 1);
        setLatestAlert(alert);
        onAlertRef.current?.(alert);

        if (Number.isFinite(longitude) && Number.isFinite(latitude)) {
          mapRef.current
            ?.getView()
            .animate({ center: fromLonLat([longitude, latitude]), duration: 500 });
        }
      };

      socket.onerror = () => setSocketStatus('error');
      socket.onclose = (event) => {
        // 1008 is the API refusing the credentials. Retrying cannot fix a wrong
        // or missing key, and a backoff loop hides the cause behind a status
        // that reads like a network blip - so say it and stop.
        if (event.code === 1008) {
          setSocketStatus('unauthorised - check VITE_P0_ALERT_TOKEN');
          console.error(
            'Alert socket rejected: the API requires P0_ALERT_API_KEY. Set VITE_P0_ALERT_TOKEN ' +
              'in frontend/.env to the same value as P0_ALERT_API_KEY in .env, then restart vite.',
          );
          return;
        }
        setSocketStatus('reconnecting');
        scheduleReconnect();
      };
    };

    const scheduleReconnect = () => {
      if (disposed) return;
      const delay = Math.min(1000 * 2 ** attempt++, 30000) * (0.75 + Math.random() * 0.5);
      reconnectRef.current = window.setTimeout(connect, delay);
    };

    connect();

    return () => {
      disposed = true;
      window.clearTimeout(reconnectRef.current);
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket) {
        socket.onclose = null;
        socket.close();
      }
    };
  }, [alertsSocketUrl, appendTrajectoryHit]);

  /* ---------------- live camera health for the open popup ---------------- */

  useEffect(() => {
    if (!selectedCamera?.id) {
      setHealth(null);
      return undefined;
    }
    const controller = new AbortController();
    let timer;

    const poll = async () => {
      try {
        setHealth(
          await request(`${apiBaseUrl}/cameras/${encodeURIComponent(selectedCamera.id)}/health`, {
            method: 'GET',
            signal: controller.signal,
          }),
        );
      } catch (error) {
        if (error.name !== 'AbortError') setHealth(null);
      } finally {
        if (!controller.signal.aborted) timer = window.setTimeout(poll, 10000);
      }
    };
    poll();

    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [apiBaseUrl, selectedCamera?.id]);

  /* ---------------- Model 2 WebRTC live stream ---------------- */

  const videoRef = useRef(null);
  const peerRef = useRef(null);
  const connectTimerRef = useRef(null);

  const closeStream = useCallback(() => {
    const peer = peerRef.current;
    if (peer) {
      // Stop the receivers before closing: without this the decoder can hold
      // the last frame and the media session lingers server-side.
      peer.getReceivers?.().forEach((receiver) => receiver.track?.stop());
      peer.close();
    }
    peerRef.current = null;
    if (connectTimerRef.current) {
      clearTimeout(connectTimerRef.current);
      connectTimerRef.current = null;
    }
    if (videoRef.current) videoRef.current.srcObject = null;
    setMediaStream(null);
    setMediaInfo(null);
    setStreamState({ status: 'idle', error: null });
  }, []);

  // Published for the map click handler, which cannot depend on closeStream
  // directly without re-creating the map.
  useEffect(() => {
    closeStreamRef.current = closeStream;
  }, [closeStream]);

  const requestLiveStream = useCallback(async () => {
    if (!selectedCamera?.id) return;
    closeStream();
    setStreamState({ status: 'connecting', error: null });

    try {
      const peer = new RTCPeerConnection({
        iceServers: [{ urls: 'stun:stun.l.google.com:19302' }],
      });
      peerRef.current = peer;
      peer.addTransceiver('video', { direction: 'recvonly' });
      peer.addTransceiver('audio', { direction: 'recvonly' });

      // One stream for the whole session, built here rather than taken from the
      // event. event.streams is populated from the answer's msid, which a WHEP
      // server is not obliged to send - and when it is absent, streams[0] is
      // undefined, srcObject becomes undefined, and the tile stays black while
      // the UI happily reports "live". Adding the track ourselves cannot fail
      // that way.
      const stream = new MediaStream();
      peer.ontrack = (event) => {
        if (peerRef.current !== peer) return; // superseded by a newer request
        const [remote] = event.streams;
        if (remote) {
          remote.getTracks().forEach((track) => {
            if (!stream.getTracks().includes(track)) stream.addTrack(track);
          });
        } else {
          stream.addTrack(event.track);
        }
        setMediaStream(stream);
        // Only a video track means there is a picture to show. Flipping to
        // "live" on an audio-first track would report success while the tile is
        // still empty.
        if (event.track.kind === 'video') {
          if (connectTimerRef.current) {
            clearTimeout(connectTimerRef.current);
            connectTimerRef.current = null;
          }
          setStreamState({ status: 'live', error: null });
        }
      };

      // Signalling can succeed while media never arrives. Without this the
      // button sits disabled on "Negotiating…" forever, which reads as a hung
      // console rather than a camera that did not answer.
      connectTimerRef.current = setTimeout(() => {
        if (peerRef.current !== peer) return;
        setStreamState({
          status: 'error',
          error: 'no video track arrived within 20s',
        });
      }, 20_000);

      // Without this the UI sits on "Negotiating..." forever whenever
      // signalling succeeds but media never arrives - no timeout, no error,
      // button disabled. Surface the failure instead.
      peer.onconnectionstatechange = () => {
        if (peerRef.current !== peer) return; // superseded by a newer request
        if (peer.connectionState === 'failed') {
          setStreamState({
            status: 'error',
            error: 'media connection failed (ICE/DTLS did not establish)',
          });
        } else if (peer.connectionState === 'disconnected') {
          setStreamState({ status: 'error', error: 'media connection lost' });
        }
      };

      const offer = await peer.createOffer();
      await peer.setLocalDescription(offer);

      const answer = await api.post(`${webrtcBaseUrl}/webrtc/offer`, {
        camera_id: selectedCamera.id,
        sdp: peer.localDescription.sdp,
        type: peer.localDescription.type,
      });
      await peer.setRemoteDescription({ type: answer.type || 'answer', sdp: answer.sdp });

      // Ask the media server what it is actually sending. The answer SDP does
      // not say, and the codec is the difference between "camera is dead" and
      // "this browser cannot decode what the camera sends".
      api
        .get(`${webrtcBaseUrl}/streams/${selectedCamera.id}/state`)
        .then((state) => {
          if (!state || peerRef.current !== peer) return;
          setMediaInfo((current) => ({ ...current, tracks: state.tracks || [] }));
        })
        .catch(() => {
          /* Statistics are a convenience; never fail a working stream over them. */
        });
    } catch (error) {
      closeStream();
      setStreamState({ status: 'error', error: error.message });
    }
  }, [closeStream, selectedCamera?.id, webrtcBaseUrl]);

  // Attach the stream once both it and the element exist. Doing this in an
  // effect rather than in ontrack means the assignment cannot be lost to
  // mount ordering, and gives somewhere to report a blocked autoplay: a
  // muted video normally plays on its own, but a rejected play() would
  // otherwise be a silent black tile.
  useEffect(() => {
    const video = videoRef.current;
    if (!video || !mediaStream) return;
    if (video.srcObject !== mediaStream) video.srcObject = mediaStream;
    video.play().catch((error) => {
      if (error.name === 'AbortError') return; // superseded by the next load
      setStreamState({
        status: 'error',
        error: `browser refused to play the stream (${error.name})`,
      });
    });
  }, [mediaStream, streamState.status]);

  // While a stream is live, read the browser's own decode counters. Bytes
  // arriving with framesDecoded stuck at zero is the signature of an
  // undecodable codec, and is otherwise indistinguishable from a dead camera.
  useEffect(() => {
    if (streamState.status !== 'live') return undefined;
    const peer = peerRef.current;
    if (!peer?.getStats) return undefined;

    let disposed = false;
    let previous = null;

    const sample = async () => {
      let report;
      try {
        report = await peer.getStats();
      } catch {
        return;
      }
      if (disposed || peerRef.current !== peer) return;

      let inbound = null;
      let codec = null;
      report.forEach((entry) => {
        if (entry.type === 'inbound-rtp' && entry.kind === 'video') inbound = entry;
      });
      if (inbound?.codecId) {
        const entry = report.get(inbound.codecId);
        // mimeType is "video/H264"; the half after the slash is the name.
        if (entry?.mimeType) codec = entry.mimeType.split('/').pop();
      }
      if (!inbound) return;

      const elapsed = previous ? (inbound.timestamp - previous.timestamp) / 1000 : 0;
      const fps =
        elapsed > 0 ? Math.round((inbound.framesDecoded - previous.framesDecoded) / elapsed) : null;
      previous = inbound;

      setMediaInfo((current) => ({
        ...current,
        codec,
        bytesReceived: inbound.bytesReceived || 0,
        framesDecoded: inbound.framesDecoded || 0,
        width: inbound.frameWidth || null,
        height: inbound.frameHeight || null,
        fps,
      }));
    };

    sample();
    const timer = setInterval(sample, 2000);
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  }, [streamState.status]);

  useEffect(() => closeStream, [closeStream]);

  const closePopup = useCallback(() => {
    overlayRef.current?.setPosition(undefined);
    selectedFeatureRef.current?.set('__selected', false);
    selectedFeatureRef.current = null;
    closeStream();
    setSelectedCamera(null);
  }, [closeStream]);

  // One sentence describing what the tile is doing, so a black picture is never
  // unexplained. Codec names come from MediaMTX (what is sent) and from the
  // browser's stats (what was decoded); they agree unless the browser cannot
  // decode it, which is exactly the case worth naming.
  const mediaSummary = useMemo(() => {
    if (!mediaInfo) return null;
    const { codec, tracks, bytesReceived = 0, framesDecoded = 0, width, height, fps } = mediaInfo;
    const sent = (tracks || []).join(', ');
    const name = codec || sent || 'unknown codec';

    if (framesDecoded > 0) {
      const size = width && height ? ` · ${width}×${height}` : '';
      const rate = fps ? ` · ${fps} fps` : '';
      return { text: `${name}${size}${rate}`, warn: false };
    }
    if (bytesReceived > 0) {
      const megabytes = (bytesReceived / 1_000_000).toFixed(1);
      // H.265 is the usual answer: MediaMTX does not transcode for WebRTC and
      // desktop Chrome will not decode it there.
      const cause = /265|hevc/i.test(sent || name)
        ? `this browser cannot decode ${sent || name} over WebRTC`
        : 'no frames decoded';
      return { text: `receiving ${megabytes} MB, 0 frames decoded — ${cause}`, warn: true };
    }
    return { text: `${name} · waiting for frames`, warn: false };
  }, [mediaInfo]);

  const healthBadge = useMemo(() => {
    const status = String(health?.status || selectedCamera?.status || 'UNKNOWN').toUpperCase();
    return { status, colour: HEALTH_COLOURS[status] || '#6b7280' };
  }, [health, selectedCamera]);

  /* ---------------- render ---------------- */

  return (
    <div style={styles.root}>
      <div ref={mapContainerRef} style={styles.map} />

      <div style={styles.hud}>
        <div style={styles.hudRow}>
          <span style={{ ...styles.dot, background: socketStatus === 'live' ? '#17a94b' : '#f0a020' }} />
          <strong>P0 CHANNEL</strong>
          <span style={styles.muted}>{socketStatus}</span>
        </div>
        <div style={styles.hudRow}>
          <span style={styles.muted}>Cameras</span>
          {registryState.status === 'unauthorised' ? (
            <strong style={{ color: '#fca5a5' }}>not signed in</strong>
          ) : registryState.status === 'error' ? (
            <strong style={{ color: '#fca5a5' }}>registry unreachable</strong>
          ) : (
            <>
              <strong>{registryState.status === 'loading' ? '…' : registryState.drawn}</strong>
              {/* A row without coordinates is silently undrawable, so say so. */}
              {registryState.received > registryState.drawn && (
                <span style={styles.muted}>
                  ({registryState.received - registryState.drawn} without coordinates)
                </span>
              )}
            </>
          )}
        </div>
        <div style={styles.hudRow}>
          <span style={styles.muted}>Alerts</span>
          <strong>{alertCount}</strong>
          <span style={styles.muted}>Track points</span>
          <strong>{trajectoryRef.current.length}</strong>
        </div>
        {latestAlert && (
          <div style={styles.latest}>
            <strong>{latestAlert.classification}</strong> · {latestAlert.plate_number || '—'} ·{' '}
            {latestAlert.camera_id || '—'}
          </div>
        )}
        <div style={styles.hudRow}>
          <button type="button" onClick={fitToCameras} style={styles.secondaryButton}>
            Fit cameras
          </button>
          <button type="button" onClick={clearTrajectory} style={styles.secondaryButton}>
            Clear trajectory
          </button>
        </div>
        <div style={styles.legend}>
          {['POLICE', 'RTO', 'CIVIL_SUPPLIES'].map((dept) => (
            <span key={dept} style={styles.legendItem}>
              <span style={{ ...styles.dot, background: departmentColour(dept) }} />
              {dept.replace('_', ' ')}
            </span>
          ))}
        </div>
      </div>

      <div ref={popupContainerRef} style={styles.popup}>
        {selectedCamera && (
          <>
            <div style={styles.popupHeader}>
              <span style={{ ...styles.dot, background: departmentColour(selectedCamera.department_id) }} />
              <strong>{selectedCamera.global_camera_code || selectedCamera.id}</strong>
              <button type="button" onClick={closePopup} style={styles.closeButton} aria-label="Close">
                ×
              </button>
            </div>

            <dl style={styles.details}>
              <dt style={styles.dt}>Department</dt>
              <dd style={styles.dd}>{selectedCamera.department_id || '—'}</dd>
              <dt style={styles.dt}>Health</dt>
              <dd style={styles.dd}>
                <span style={{ ...styles.dot, background: healthBadge.colour }} />
                {healthBadge.status}
                {health?.last_ping_at && (
                  <span style={styles.muted}>
                    {' '}
                    · {new Date(health.last_ping_at).toLocaleTimeString()}
                  </span>
                )}
              </dd>
              <dt style={styles.dt}>Location</dt>
              <dd style={styles.dd}>
                {Number(selectedCamera.latitude).toFixed(5)}, {Number(selectedCamera.longitude).toFixed(5)}
              </dd>
              <dt style={styles.dt}>Azimuth / FOV</dt>
              <dd style={styles.dd}>
                {selectedCamera.azimuth_angle ?? '—'}° / {selectedCamera.fov_degrees ?? '—'}°
              </dd>
              <dt style={styles.dt}>VMS</dt>
              <dd style={styles.dd}>{selectedCamera.vms_vendor || '—'}</dd>
            </dl>

            {streamState.status !== 'idle' && (
              <video
                ref={videoRef}
                autoPlay
                playsInline
                muted
                style={styles.video}
              />
            )}
            {streamState.status !== 'idle' && mediaSummary && (
              <p style={mediaSummary.warn ? styles.mediaWarning : styles.mediaInfo}>
                {mediaSummary.text}
              </p>
            )}
            {streamState.status === 'error' && (
              <p style={styles.error}>Stream failed: {streamState.error}</p>
            )}

            <button
              type="button"
              onClick={streamState.status === 'live' ? closeStream : requestLiveStream}
              disabled={streamState.status === 'connecting'}
              style={styles.primaryButton}
            >
              {streamState.status === 'connecting'
                ? 'Negotiating…'
                : streamState.status === 'live'
                  ? 'Stop Live Stream'
                  : 'Request Live Stream'}
            </button>
          </>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Inline styles (kept local so the component ships as one file)       */
/* ------------------------------------------------------------------ */

const styles = {
  root: { position: 'relative', width: '100%', height: '100%', minHeight: 480 },
  map: { position: 'absolute', inset: 0 },
  hud: {
    position: 'absolute',
    top: 12,
    left: 12,
    zIndex: 2,
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
    padding: '10px 12px',
    borderRadius: 8,
    background: 'rgba(17, 24, 39, 0.85)',
    color: '#f9fafb',
    font: '12px/1.4 system-ui, sans-serif',
    boxShadow: '0 2px 10px rgba(0,0,0,0.35)',
  },
  hudRow: { display: 'flex', alignItems: 'center', gap: 8 },
  muted: { color: '#9ca3af' },
  latest: { maxWidth: 260, color: '#fca5a5' },
  legend: { display: 'flex', gap: 10, flexWrap: 'wrap', color: '#d1d5db' },
  legendItem: { display: 'flex', alignItems: 'center', gap: 4 },
  dot: { display: 'inline-block', width: 9, height: 9, borderRadius: '50%' },
  popup: {
    minWidth: 260,
    maxWidth: 320,
    padding: 12,
    borderRadius: 10,
    background: '#ffffff',
    color: '#111827',
    font: '13px/1.45 system-ui, sans-serif',
    boxShadow: '0 6px 24px rgba(0,0,0,0.25)',
  },
  popupHeader: { display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 },
  closeButton: {
    marginLeft: 'auto',
    border: 'none',
    background: 'transparent',
    fontSize: 20,
    lineHeight: 1,
    cursor: 'pointer',
    color: '#6b7280',
  },
  details: { display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '4px 10px', margin: 0 },
  dt: { color: '#6b7280' },
  dd: { margin: 0, display: 'flex', alignItems: 'center', gap: 6 },
  // A frameless <video> has an intrinsic size of zero, so without a reserved
  // box a waiting stream looks like nothing rendered at all.
  video: {
    width: '100%',
    marginTop: 10,
    borderRadius: 6,
    background: '#000',
    aspectRatio: '16 / 9',
    minHeight: 120,
    objectFit: 'contain',
    display: 'block',
  },
  mediaInfo: { color: '#6b7280', fontSize: 11, margin: '6px 0 0' },
  mediaWarning: { color: '#b45309', fontSize: 11, margin: '6px 0 0' },
  error: { color: '#b91c1c', margin: '8px 0 0' },
  primaryButton: {
    marginTop: 10,
    width: '100%',
    padding: '8px 10px',
    borderRadius: 6,
    border: 'none',
    background: '#1d6fe0',
    color: '#fff',
    fontWeight: 600,
    cursor: 'pointer',
  },
  secondaryButton: {
    padding: '5px 8px',
    borderRadius: 6,
    border: '1px solid #4b5563',
    background: 'transparent',
    color: '#e5e7eb',
    cursor: 'pointer',
  },
};
