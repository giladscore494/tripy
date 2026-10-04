import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { ApiError, configureClient } from "../api/client";
import { api } from "../api/endpoints";
import type { ConfigStatus } from "../api/types";
import { tokenStore } from "./tokenStore";

/**
 * checking     probing the API with the stored token (or none)
 * locked       the API wants a Bearer token (none stored, or the stored one was refused)
 * unlocked     the API answers: with a token in production, or openly in development
 * misconfigured production without TRIPY_ACCESS_TOKEN: the server refuses everything (a server problem, no login)
 * unreachable  the API could not be reached at all
 */
export type AuthStatus = "checking" | "locked" | "unlocked" | "misconfigured" | "unreachable";

interface AuthContextValue {
  status: AuthStatus;
  /** Why the unlock screen is shown (expired / invalid access); null on a first visit. */
  notice: string | null;
  serverMessage: string | null;
  /** The API requires a token (production); false in open development mode. */
  accessRequired: boolean;
  unlock: (token: string) => Promise<boolean>;
  logout: () => void;
  retry: () => void;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export const EXPIRED_NOTICE = "Your access expired or is no longer valid. Unlock again to continue.";
export const REJECTED_NOTICE = "That access token was not accepted.";

export function AuthProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus>("checking");
  const [notice, setNotice] = useState<string | null>(null);
  const [serverMessage, setServerMessage] = useState<string | null>(null);
  const [accessRequired, setAccessRequired] = useState(false);
  // the token in memory (mirrors sessionStorage; also covers browsers where storage is blocked)
  const token = useRef<string | null>(tokenStore.get());

  const lock = useCallback((message: string | null) => {
    token.current = null;
    tokenStore.clear();
    setNotice(message);
    setStatus("locked");
  }, []);

  useEffect(() => {
    configureClient({
      getToken: () => token.current,
      onUnauthorized: () => lock(EXPIRED_NOTICE),
      onAccessNotConfigured: () => setStatus("misconfigured"),
    });
  }, [lock]);

  const probe = useCallback(async (): Promise<ConfigStatus | ApiError> => {
    try {
      return await api.configStatus();
    } catch (error) {
      return error instanceof ApiError ? error : new ApiError(0, "network_error", "The server could not be reached.");
    }
  }, []);

  const settle = useCallback((outcome: ConfigStatus | ApiError, hadToken: boolean) => {
    if (!(outcome instanceof ApiError)) {
      setAccessRequired(Boolean(outcome.access_control?.required));
      setNotice(null);
      setStatus("unlocked");
      return true;
    }
    if (outcome.status === 401) {
      lock(hadToken ? EXPIRED_NOTICE : null);
    } else if (outcome.code === "access_control_not_configured") {
      setServerMessage(outcome.message);
      setStatus("misconfigured");
    } else {
      setServerMessage(outcome.message);
      setStatus("unreachable");
    }
    return false;
  }, [lock]);

  const check = useCallback(async () => {
    setStatus("checking");
    const hadToken = Boolean(token.current);
    settle(await probe(), hadToken);
  }, [probe, settle]);

  useEffect(() => {
    void check();
  }, [check]);

  const unlock = useCallback(async (value: string) => {
    const candidate = value.trim();
    if (!candidate) return false;
    token.current = candidate;
    const outcome = await probe();
    if (outcome instanceof ApiError && outcome.status === 401) {
      lock(REJECTED_NOTICE);
      return false;
    }
    const ok = settle(outcome, true);
    if (ok) tokenStore.set(candidate);
    return ok;
  }, [lock, probe, settle]);

  const logout = useCallback(() => lock(null), [lock]);

  const value = useMemo<AuthContextValue>(() => ({
    status, notice, serverMessage, accessRequired, unlock, logout, retry: () => void check(),
  }), [status, notice, serverMessage, accessRequired, unlock, logout, check]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth outside AuthProvider");
  return value;
}
