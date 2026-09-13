import { useEffect, useState } from 'react';

import { api } from '../lib/api.js';

/**
 * Fetch an image that needs our bearer token and hand back an object URL.
 *
 * An <img src="..."> is a plain browser GET, same limitation as <video src>:
 * there is no way to attach an Authorization header to it. Every protected
 * image in this console (detection snapshots, recorded clips) goes through
 * api.js instead and is handed to the element as a blob: URL - see
 * PlaybackPage's clip-fetch effect for the video equivalent of this hook.
 */
export function useAuthedImage(path) {
  const [url, setUrl] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!path) {
      setUrl(null);
      setError(null);
      return undefined;
    }

    let objectUrl = null;
    let cancelled = false;
    setError(null);

    api
      .raw(path)
      .then((response) => response.blob())
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch((err) => {
        if (!cancelled) setError(err.detail || err.message);
      });

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [path]);

  return { url, error };
}

export default useAuthedImage;
