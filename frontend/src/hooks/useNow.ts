import { useEffect, useState } from "react";

/** The current time, ticking every `ms` while `enabled` (elapsed-time displays of active runs). */
export function useNow(enabled: boolean, ms = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!enabled) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(timer);
  }, [enabled, ms]);
  return now;
}
