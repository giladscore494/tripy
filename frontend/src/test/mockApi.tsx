// A tiny HTTP mock for the workspace tests: routes "METHOD /path" to handlers, records every call (with headers),
// never touches the network.
import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

import { Gate } from "../App";
import { AuthProvider } from "../auth/AuthProvider";
import * as F from "./fixtures";

export interface Call {
  method: string;
  path: string;
  query: URLSearchParams;
  body: unknown;
  headers: Record<string, string>;
}

export type Reply = { status?: number; body?: unknown; headers?: Record<string, string>; text?: string } | Error;
export type Handler = (call: Call) => Reply | Promise<Reply>;

export function json(body: unknown, status = 200): Reply {
  return { status, body };
}

export function apiError(status: number, code: string, message: string, extra: Record<string, unknown> = {}): Reply {
  return { status, body: { error: { code, message, ...extra } } };
}

export function defaultRoutes(): Record<string, Handler> {
  return {
    "GET /health": () => json({ status: "ok", service: "tripy" }),
    "GET /api/config/status": () => json(F.config),
    "GET /api/runs": () => json(F.runList),
    "GET /api/vehicles": () => json(F.vehicles),
    "GET /api/run-settings": () => json(F.contract),
    "GET /api/series": () => json(F.seriesList()),
  };
}

export function mockApi(overrides: Record<string, Handler> = {}) {
  const routes: Record<string, Handler> = { ...defaultRoutes(), ...overrides };
  const calls: Call[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = new URL(String(input), "http://localhost");
    const method = (init.method ?? "GET").toUpperCase();
    const headers = Object.fromEntries(Object.entries((init.headers ?? {}) as Record<string, string>));
    const call: Call = { method, path: url.pathname, query: url.searchParams, headers,
                         body: init.body ? JSON.parse(String(init.body)) : undefined };
    calls.push(call);
    const handler = routes[`${method} ${url.pathname}`]
      ?? Object.entries(routes).find(([key]) => {
        const [m, pattern] = key.split(" ");
        return m === method && pattern.includes("*") && new RegExp(`^${pattern.replace(/\*/g, "[^/]+")}$`).test(url.pathname);
      })?.[1];
    const reply = handler ? await handler(call) : apiError(404, "not_found", `no mock for ${method} ${url.pathname}`);
    if (reply instanceof Error) throw reply;
    const status = reply.status ?? 200;
    const text = reply.text ?? (reply.body === undefined ? "" : JSON.stringify(reply.body));
    return new Response(text, { status, headers: { "Content-Type": "application/json", ...(reply.headers ?? {}) } });
  });
  vi.stubGlobal("fetch", fetchMock);
  return {
    calls,
    routes,
    set(key: string, handler: Handler) {
      routes[key] = handler;
    },
    callsTo(method: string, path: string) {
      return calls.filter((c) => c.method === method && c.path === path);
    },
  };
}

export function renderApp(path = "/"): ReturnType<typeof render> {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <Gate />
      </AuthProvider>
    </MemoryRouter>,
  );
}

export function renderWithRouter(ui: ReactElement, path = "/") {
  return render(<MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>);
}
