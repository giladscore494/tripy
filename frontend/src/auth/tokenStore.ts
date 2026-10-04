// The access token lives in sessionStorage only (this browser tab's session; never localStorage, never a URL, never
// a cookie, never logged). Storage can be unavailable (private mode, blocked site data): every access is guarded.

const KEY = "tripy.access";

export const tokenStore = {
  get(): string | null {
    try {
      return window.sessionStorage.getItem(KEY);
    } catch {
      return null;
    }
  },
  set(token: string): void {
    try {
      window.sessionStorage.setItem(KEY, token);
    } catch {
      /* storage unavailable: the token stays in memory for this page only */
    }
  },
  clear(): void {
    try {
      window.sessionStorage.removeItem(KEY);
    } catch {
      /* nothing stored */
    }
  },
};
