/**
 * One camera's live WebRTC stream, as a hook.
 *
 * Extracted from GISMap's popup so the map and the video wall share a single
 * player. Everything here was learned the hard way against MediaMTX and is
 * worth keeping in one place rather than reimplementing per view:
 *
 *   - the MediaStream is built locally, because a WHEP answer is not obliged to
 *     carry an msid and `event.streams[0]` is then undefined - which attaches
 *     nothing while the UI cheerfully reports "live";
 *   - srcObject is assigned by an effect, not inside ontrack, so the assignment
 *     cannot be lost to element mount ordering;
 *   - play() is called explicitly and a rejection is surfaced, since a blocked
 *     autoplay is otherwise a silent black tile;
 *   - decode counters are sampled, because bytes arriving with framesDecoded
 *     stuck at zero (an undecodable codec, usually H.265) looks exactly like a
 *     dead camera.
 *
 * The wall adds two needs a single popup never had: it must recover on its own,
 * and it must not keep the grid's connections open when nobody is looking. Both
 * live here rather than in the tile, so the map inherits them too.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { api } from '../lib/api.js';

// Signalling can succeed while media never arrives. Without a deadline the UI
// waits forever on a camera that will never answer.
const NO_VIDEO_TIMEOUT_MS = 20_000;
const STATS_INTERVAL_MS = 2000;

// Retry backoff. Starts quick enough to ride out a blip, ends slow enough that
// a camera that is genuinely down is not hammered - nor, more importantly, is
// the external grid, which serves every reconnect as a fresh RTSP pull.
const RETRY_BASE_MS = 2000;
const RETRY_MAX_MS = 30_000;

/** True while this document is visible; used to release streams nobody watches. */
function useDocumentVisible() {
  const [visible, setVisible] = useState(
    () => typeof document === 'undefined' || document.visibilityState !== 'hidden',
  );

  useEffect(() => {
    const onChange = () => setVisible(document.visibilityState !== 'hidden');
    document.addEventListener('visibilitychange', onChange);
    return () => document.removeEventListener('visibilitychange', onChange);
  }, []);

  return visible;
}

/**
 * @param {object}  options
 * @param {string}  options.cameraId       registry UUID
 * @param {string} [options.webrtcBaseUrl] defaults to '/api/v2'
 * @param {boolean} [options.enabled]      false tears the session down
 * @param {boolean} [options.autoRetry]    reconnect with backoff while enabled
 * @param {boolean} [options.pauseWhenHidden] release the stream on a hidden tab
 */
export function useCameraStream({
  cameraId,
  webrtcBaseUrl = '/api/v2',
  enabled = false,
  autoRetry = false,
  pauseWhenHidden = false,
} = {}) {
  const videoRef = useRef(null);
  const peerRef = useRef(null);
  const timeoutRef = useRef(null);
  const retryRef = useRef(null);
  const attemptRef = useRef(0);

  const [status, setStatus] = useState('idle'); // idle | connecting | live | error
  const [error, setError] = useState(null);
  const [stream, setStream] = useState(null);
  const [info, setInfo] = useState(null);

  const visible = useDocumentVisible();
  const active = enabled && (!pauseWhenHidden || visible);

  const clearTimers = useCallback(() => {
    if (timeoutRef.current) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
    }
    if (retryRef.current) {
      clearTimeout(retryRef.current);
      retryRef.current = null;
    }
  }, []);

  const stop = useCallback(() => {
    const peer = peerRef.current;
    if (peer) {
      // Stop the receivers before closing: without this the decoder can hold
      // the last frame and the media session lingers server-side, which on the
      // external grid means a pull nobody is watching.
      peer.getReceivers?.().forEach((receiver) => receiver.track?.stop());
      peer.close();
    }
    peerRef.current = null;
    clearTimers();
    if (videoRef.current) videoRef.current.srcObject = null;
    setStream(null);
    setInfo(null);
    setError(null);
    setStatus('idle');
  }, [clearTimers]);

  // Declared as a ref so the connect effect below does not have to list it as a
  // dependency and re-run - reconnecting the camera - on every render.
  const startRef = useRef(null);

  const start = useCallback(async () => {
    if (!cameraId) return;
    stop();
    setStatus('connecting');

    let peer;
    try {
      peer = new RTCPeerConnection({
        iceServers: [{ urls: 'stun:stun.l.google.com:19302' }],
      });
      peerRef.current = peer;
      peer.addTransceiver('video', { direction: 'recvonly' });
      peer.addTransceiver('audio', { direction: 'recvonly' });

      const media = new MediaStream();
      peer.ontrack = (event) => {
        if (peerRef.current !== peer) return; // superseded by a newer attempt
        const [remote] = event.streams;
        if (remote) {
          remote.getTracks().forEach((track) => {
            if (!media.getTracks().includes(track)) media.addTrack(track);
          });
        } else {
          media.addTrack(event.track);
        }
        setStream(media);
        // Only a video track means there is a picture. Reporting "live" on an
        // audio-first track would claim success over an empty tile.
        if (event.track.kind === 'video') {
          clearTimers();
          attemptRef.current = 0; // a good connection resets the backoff
          setError(null);
          setStatus('live');
        }
      };

      peer.onconnectionstatechange = () => {
        if (peerRef.current !== peer) return;
        if (peer.connectionState === 'failed') {
          setError('media connection failed (ICE/DTLS did not establish)');
          setStatus('error');
        } else if (peer.connectionState === 'disconnected') {
          setError('media connection lost');
          setStatus('error');
        }
      };

      timeoutRef.current = setTimeout(() => {
        if (peerRef.current !== peer) return;
        setError('no video track arrived within 20s');
        setStatus('error');
      }, NO_VIDEO_TIMEOUT_MS);

      const offer = await peer.createOffer();
      await peer.setLocalDescription(offer);

      const answer = await api.post(`${webrtcBaseUrl}/webrtc/offer`, {
        camera_id: cameraId,
        sdp: peer.localDescription.sdp,
        type: peer.localDescription.type,
      });
      if (peerRef.current !== peer) return; // torn down while negotiating
      await peer.setRemoteDescription({ type: answer.type || 'answer', sdp: answer.sdp });

      // What the media server says it is sending. The answer SDP does not say,
      // and a codec this browser cannot decode is otherwise indistinguishable
      // from a dead camera: both are a black rectangle.
      api
        .get(`${webrtcBaseUrl}/streams/${cameraId}/state`)
        .then((state) => {
          if (!state || peerRef.current !== peer) return;
          setInfo((current) => ({ ...current, tracks: state.tracks || [] }));
        })
        .catch(() => {
          /* Diagnostics are a convenience; never fail a working stream over them. */
        });
    } catch (failure) {
      if (peerRef.current === peer) {
        peerRef.current = null;
        peer?.close();
      }
      clearTimers();
      // ApiError carries the API's own sentence - "camera is INACTIVE, not
      // ACTIVE" says far more than "request failed with status 409".
      setError(failure.detail || failure.message);
      setStatus('error');
    }
  }, [cameraId, clearTimers, stop, webrtcBaseUrl]);

  startRef.current = start;

  // Connect when asked, tear down when not. Keyed on the camera so a tile that
  // is reassigned to another camera swaps cleanly.
  useEffect(() => {
    if (!active || !cameraId) {
      stop();
      return undefined;
    }
    attemptRef.current = 0;
    startRef.current?.();
    return () => stop();
  }, [active, cameraId, stop]);

  // Recover on its own. This is what makes a wall continuous rather than a grid
  // of boxes that died at the first blip and stayed dead.
  useEffect(() => {
    if (!autoRetry || !active || status !== 'error') return undefined;
    const delay = Math.min(RETRY_BASE_MS * 2 ** attemptRef.current, RETRY_MAX_MS);
    attemptRef.current += 1;
    retryRef.current = setTimeout(() => startRef.current?.(), delay);
    return () => {
      if (retryRef.current) {
        clearTimeout(retryRef.current);
        retryRef.current = null;
      }
    };
  }, [autoRetry, active, status]);

  // Attach here rather than in ontrack: the element may mount after the track
  // arrives, and a rejected play() needs somewhere to be reported.
  useEffect(() => {
    const video = videoRef.current;
    if (!video || !stream) return;
    if (video.srcObject !== stream) video.srcObject = stream;
    video.play().catch((failure) => {
      if (failure.name === 'AbortError') return; // superseded by the next load
      setError(`browser refused to play the stream (${failure.name})`);
      setStatus('error');
    });
  }, [stream, status]);

  // Decode counters, sampled while live.
  useEffect(() => {
    if (status !== 'live') return undefined;
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
      if (!inbound) return;
      if (inbound.codecId) {
        const entry = report.get(inbound.codecId);
        // mimeType is "video/H264"; the half after the slash is the name.
        if (entry?.mimeType) codec = entry.mimeType.split('/').pop();
      }

      const elapsed = previous ? (inbound.timestamp - previous.timestamp) / 1000 : 0;
      const fps =
        elapsed > 0 ? Math.round((inbound.framesDecoded - previous.framesDecoded) / elapsed) : null;
      previous = inbound;

      setInfo((current) => ({
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
    const timer = setInterval(sample, STATS_INTERVAL_MS);
    return () => {
      disposed = true;
      clearInterval(timer);
    };
  }, [status]);

  useEffect(() => stop, [stop]);

  /**
   * One sentence describing what the tile is doing, so a black picture is never
   * unexplained. Codec names come from MediaMTX (what is sent) and from the
   * browser's stats (what was decoded); they agree unless the browser cannot
   * decode it - which is exactly the case worth naming.
   */
  const summary = useMemo(() => {
    if (!info) return null;
    const { codec, tracks, bytesReceived = 0, framesDecoded = 0, width, height, fps } = info;
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
  }, [info]);

  return { videoRef, status, error, stream, info, summary, start, stop, paused: enabled && !active };
}

export default useCameraStream;
