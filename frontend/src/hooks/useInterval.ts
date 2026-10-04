import { useEffect, useRef } from "react";

/** Call `callback` every `ms` (null = stopped); skipped while the tab is hidden. */
export function useInterval(callback: () => void, ms: number | null): void {
  const saved = useRef(callback);
  saved.current = callback;
  useEffect(() => {
    if (!ms) return undefined;
    const timer = window.setInterval(() => {
      if (typeof document !== "undefined" && document.hidden) return;
      saved.current();
    }, ms);
    return () => window.clearInterval(timer);
  }, [ms]);
}
