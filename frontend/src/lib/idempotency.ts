// One idempotency key per deliberate submission: created when the user submits, reused for every retry of THAT
// submission (a network retry or a double click returns the same run), cleared once the submission resolves.
import { useCallback, useRef } from "react";

export function newKey(prefix: string): string {
  const random = typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}:${random}`;
}

export function useSubmissionKey(prefix: string) {
  const key = useRef<string | null>(null);
  const inFlight = useRef(false);
  /** Run `submit` unless a submission is already in flight (synchronous: a double click cannot fire twice before
   *  React re-renders the disabled button). */
  const guard = useCallback(async (submit: () => Promise<void>) => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      await submit();
    } finally {
      inFlight.current = false;
    }
  }, []);
  const current = useCallback(() => {
    if (!key.current) key.current = newKey(prefix);
    return key.current;
  }, [prefix]);
  const resolve = useCallback(() => {
    key.current = null;
  }, []);
  return { current, resolve, guard };
}
