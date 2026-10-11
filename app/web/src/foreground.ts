import { useEffect, useRef } from 'react';

/** Runs `fn` each time the app comes back to the foreground (the WebView becomes visible again). */
export function useOnForeground(fn: () => void): void {
  const latest = useRef(fn);
  latest.current = fn;
  useEffect(() => {
    const onChange = () => {
      if (document.visibilityState === 'visible') latest.current();
    };
    document.addEventListener('visibilitychange', onChange);
    return () => document.removeEventListener('visibilitychange', onChange);
  }, []);
}
