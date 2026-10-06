// The ONE HTTP client of the workspace. It attaches `Authorization: Bearer <token>` to every request (never a URL
// parameter), maps the backend's error envelope {"error": {"code", "message", ...}} to ApiError, and reports
// authentication outcomes to the auth layer. It never logs a token, a request header or a response body.

import type { ApiErrorBody } from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export interface ClientHooks {
  /** The current access token (sessionStorage); null in development when the API needs none. */
  getToken: () => string | null;
  /** A protected request answered 401: the token is missing, invalid or no longer accepted. */
  onUnauthorized: () => void;
  /** Production without TRIPY_ACCESS_TOKEN: the server refuses every /api route (not a login problem). */
  onAccessNotConfigured: () => void;
}

let hooks: ClientHooks = {
  getToken: () => null,
  onUnauthorized: () => undefined,
  onAccessNotConfigured: () => undefined,
};

export function configureClient(next: Partial<ClientHooks>): void {
  hooks = { ...hooks, ...next };
}

const MAX_MESSAGE = 400;

function cleanMessage(message: unknown, fallback: string): string {
  // The backend never sends tracebacks; still, only a short single-paragraph message is ever shown.
  if (typeof message !== "string" || !message.trim()) return fallback;
  const text = message.trim();
  if (/Traceback \(most recent call last\)/.test(text)) return fallback;
  return text.length > MAX_MESSAGE ? `${text.slice(0, MAX_MESSAGE)}…` : text;
}

const STATUS_FALLBACK: Record<number, string> = {
  400: "The request was not accepted.",
  401: "Your access token is missing, invalid or has expired.",
  403: "This action is not allowed.",
  404: "Not found.",
  409: "The request conflicts with the current state.",
  422: "The request is invalid.",
  500: "The server hit an unexpected error.",
  502: "The server is unavailable.",
  503: "The server is not ready.",
  504: "The server took too long to answer.",
};

async function toApiError(response: Response): Promise<ApiError> {
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  const fallback = STATUS_FALLBACK[response.status] ?? `The server answered ${response.status}.`;
  const envelope = (body as ApiErrorBody | null)?.error;
  if (envelope && typeof envelope === "object") {
    const { code, message, ...details } = envelope;
    return new ApiError(response.status, typeof code === "string" ? code : "http_error",
      cleanMessage(message, fallback), details);
  }
  return new ApiError(response.status, "http_error", fallback);
}

export interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  query?: Record<string, string | number | boolean | null | undefined | (string | number)[]>;
}

export function buildUrl(path: string, query?: RequestOptions["query"]): string {
  if (!query) return path;
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) value.forEach((v) => params.append(key, String(v)));
    else params.append(key, String(value));
  }
  const text = params.toString();
  return text ? `${path}?${text}` : path;
}

async function send(path: string, options: RequestOptions, accept: string): Promise<Response> {
  const headers: Record<string, string> = { Accept: accept };
  const token = hooks.getToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  let body: string | undefined;
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  let response: Response;
  try {
    response = await fetch(buildUrl(path, options.query), {
      method: options.method ?? (body !== undefined ? "POST" : "GET"),
      headers,
      body,
      signal: options.signal,
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch (error) {
    if ((error as { name?: string })?.name === "AbortError") throw error;
    throw new ApiError(0, "network_error", "The TRIPY server could not be reached.");
  }
  if (!response.ok) {
    const apiError = await toApiError(response);
    if (response.status === 401) hooks.onUnauthorized();
    else if (response.status === 503 && apiError.code === "access_control_not_configured") {
      hooks.onAccessNotConfigured();
    }
    throw apiError;
  }
  return response;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await send(path, options, "application/json");
  if (response.status === 204) return undefined as T;
  try {
    return (await response.json()) as T;
  } catch {
    throw new ApiError(response.status, "invalid_response", "The server sent an unreadable response.");
  }
}

function filenameFrom(disposition: string | null, fallback: string): string {
  const match = disposition?.match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
  const name = match?.[1] ? decodeURIComponent(match[1]) : fallback;
  return name.replace(/[\\/]/g, "_");
}

/** Fetch a canonical backend export through the authenticated client and hand it to the browser as a download.
 *  The token stays in the Authorization header; the browser only ever sees a blob: URL. */
export async function download(path: string, fallbackName: string, query?: RequestOptions["query"]): Promise<string> {
  const response = await send(path, { query }, "*/*");
  const blob = await response.blob();
  const name = filenameFrom(response.headers.get("Content-Disposition"), fallbackName);
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = name;
    anchor.rel = "noopener";
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  return name;
}
