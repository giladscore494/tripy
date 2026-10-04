import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { EXPIRED_NOTICE, REJECTED_NOTICE } from "../auth/AuthProvider";
import { apiError, json, mockApi, renderApp } from "./mockApi";
import * as F from "./fixtures";

const TOKEN = "prod-access-token-0123456789";

function productionApi() {
  // the API accepts exactly TOKEN as a Bearer header (like src/api/auth.py)
  const guard = (body: unknown) => (call: { headers: Record<string, string> }) =>
    call.headers.Authorization === `Bearer ${TOKEN}` ? json(body)
      : apiError(401, "unauthorized", "Missing or invalid access token.");
  return mockApi({
    "GET /api/config/status": guard({ ...F.config, environment: "production",
                                      access_control: { required: true, configured: true } }),
    "GET /api/runs": guard(F.runList),
  });
}

describe("authentication", () => {
  it("shows the unlock screen, rejects a wrong token and unlocks with the right one", async () => {
    const api = productionApi();
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "Unlock workspace" })).toBeInTheDocument();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Access token"), "wrong-token");
    await user.click(screen.getByRole("button", { name: "Unlock" }));
    expect(await screen.findByText(REJECTED_NOTICE)).toBeInTheDocument();
    expect(window.sessionStorage.getItem("tripy.access")).toBeNull();

    await user.type(screen.getByLabelText("Access token"), TOKEN);
    await user.click(screen.getByRole("button", { name: "Unlock" }));
    expect(await screen.findByRole("heading", { name: "Research Intelligence Workspace" })).toBeInTheDocument();
    // sessionStorage only, never localStorage; the token never appears in a URL
    expect(window.sessionStorage.getItem("tripy.access")).toBe(TOKEN);
    expect(window.localStorage.length).toBe(0);
    expect(api.calls.every((c) => !c.path.includes(TOKEN) && !c.query.toString().includes(TOKEN))).toBe(true);
    await waitFor(() => expect(api.callsTo("GET", "/api/runs").length).toBeGreaterThan(0));
    expect(api.callsTo("GET", "/api/runs").every((c) => c.headers.Authorization === `Bearer ${TOKEN}`)).toBe(true);
  });

  it("restores the session from sessionStorage and locks with the expired notice on a later 401", async () => {
    window.sessionStorage.setItem("tripy.access", TOKEN);
    const api = productionApi();
    renderApp("/runs");
    expect(await screen.findByRole("heading", { name: "Runs" })).toBeInTheDocument();
    // the server stops accepting the token: the next protected request answers 401
    api.set("GET /api/runs/*", () => apiError(401, "unauthorized", "Missing or invalid access token."));
    const user = userEvent.setup();
    await user.click(await screen.findByText(F.MULTI_ID));
    expect(await screen.findByText(EXPIRED_NOTICE)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Unlock workspace" })).toBeInTheDocument();
    expect(window.sessionStorage.getItem("tripy.access")).toBeNull();
  });

  it("locks the session on demand", async () => {
    window.sessionStorage.setItem("tripy.access", TOKEN);
    productionApi();
    renderApp("/");
    await screen.findByRole("heading", { name: "Research Intelligence Workspace" });
    await userEvent.setup().click(screen.getByRole("button", { name: /Lock/ }));
    expect(await screen.findByRole("heading", { name: "Unlock workspace" })).toBeInTheDocument();
    expect(window.sessionStorage.getItem("tripy.access")).toBeNull();
  });

  it("returns to the unlock screen with the expired notice when a request answers 401", async () => {
    window.sessionStorage.setItem("tripy.access", "revoked-token");
    productionApi();
    renderApp("/");
    expect(await screen.findByText(EXPIRED_NOTICE)).toBeInTheDocument();
    expect(window.sessionStorage.getItem("tripy.access")).toBeNull();
  });

  it("works without a token when the API needs none (development)", async () => {
    const api = mockApi();
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "Research Intelligence Workspace" })).toBeInTheDocument();
    expect(api.calls.every((c) => c.headers.Authorization === undefined)).toBe(true);
    expect(screen.queryByRole("button", { name: /Lock/ })).not.toBeInTheDocument();
  });

  it("shows a server-configuration error, not a login loop, when access control is not configured", async () => {
    mockApi({ "GET /api/config/status": () => apiError(503, "access_control_not_configured",
                                                       "Production access control is not configured.") });
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "Server access control is not configured" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Access token")).not.toBeInTheDocument();
  });

  it("reports an unreachable server", async () => {
    mockApi({ "GET /api/config/status": () => new TypeError("Failed to fetch") });
    renderApp("/");
    expect(await screen.findByRole("heading", { name: "The TRIPY server is unreachable" })).toBeInTheDocument();
  });
});
