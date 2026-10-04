import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { defaultsOf, overridesOf, settingError } from "../components/run/RunSettingsForm";
import { apiError, json, mockApi, renderApp, type Call } from "./mockApi";
import * as F from "./fixtures";

const started = (runId: string) => json({ run_id: runId, created: true, message: "", warnings: [], run: null,
                                          settings_overridden: [] }, 201);

async function openNewResearch() {
  renderApp("/research/new");
  await screen.findByRole("heading", { name: "New research" });
  await screen.findByRole("listbox", { name: "Vehicles" });
  return userEvent.setup();
}

function lastStart(calls: Call[]) {
  const posts = calls.filter((c) => c.method === "POST" && c.path === "/api/runs");
  return posts[posts.length - 1]?.body as Record<string, unknown>;
}

describe("new research", () => {
  it("starts one vehicle from the searchable picker and opens the run", async () => {
    const api = mockApi({ "POST /api/runs": () => started("run-one"),
                          "GET /api/runs/run-one": () => json({ ...F.completedRun, run_id: "run-one" }) });
    const user = await openNewResearch();
    const start = screen.getByRole("button", { name: /Start research/ });
    expect(start).toBeDisabled();                                    // nothing chosen yet
    await user.type(screen.getByLabelText("Search vehicles"), "CORE");
    const options = within(screen.getByRole("listbox", { name: "Vehicles" })).getAllByRole("option");
    expect(options).toHaveLength(1);
    await user.click(within(options[0]).getByRole("button"));
    await user.click(start);
    await waitFor(() => expect(lastStart(api.calls)).toBeDefined());
    const body = lastStart(api.calls);
    expect(body).toMatchObject({ scope: "one", record_id: "101136", profile: F.config.default_profile });
    expect(body.settings).toBeUndefined();                           // untouched settings: server defaults
    expect(String(body.idempotency_key)).toMatch(/^run:/);
    expect(await screen.findByText("run-one")).toBeInTheDocument();
  });

  it("starts a manufacturer and the whole benchmark with friendly scope labels", async () => {
    const api = mockApi({ "POST /api/runs": () => started("run-x"), "GET /api/runs/run-x": () => json(F.multiRun) });
    const user = await openNewResearch();
    await user.click(screen.getByRole("radio", { name: /Manufacturer/ }));
    await user.selectOptions(screen.getByLabelText("Manufacturer"), "טויוטה");
    expect(screen.getByText("2 benchmark vehicles will be researched.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Start research/ }));
    await waitFor(() => expect(lastStart(api.calls)).toMatchObject({ scope: "manufacturer", manufacturer: "טויוטה" }));
  });

  it("shows how many vehicles the whole benchmark runs", async () => {
    const idle = { total: 1, runs: F.runList.runs.filter((r) => !r.active) };
    const api = mockApi({ "POST /api/runs": () => started("run-all"), "GET /api/runs/run-all": () => json(F.multiRun),
                          "GET /api/runs": () => json(idle) });
    const user = await openNewResearch();
    await user.click(screen.getByRole("radio", { name: /Whole benchmark/ }));
    expect(screen.getByText(`${F.vehicles.vehicles.length} benchmark vehicles will run`)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Start research/ }));
    await waitFor(() => expect(lastStart(api.calls)).toMatchObject({ scope: "all" }));
    expect(lastStart(api.calls).record_id).toBeUndefined();
  });

  it("sends only the per-run settings the user changed, validated against the contract", async () => {
    const api = mockApi({ "POST /api/runs": () => started("run-s"), "GET /api/runs/run-s": () => json(F.completedRun) });
    const user = await openNewResearch();
    await user.click(screen.getAllByRole("option")[3].querySelector("button")!);
    await user.click(await screen.findByRole("button", { name: /Per-run settings/ }));
    await user.selectOptions(screen.getByLabelText("Run profile"), "custom");
    const workers = screen.getByLabelText(/Vehicle workers/);
    await user.clear(workers);
    await user.type(workers, "99");                                   // above the contract's maximum
    expect(screen.getByText(/Maximum 50/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Start research/ })).toBeDisabled();
    await user.clear(workers);
    expect(screen.getByText("Required")).toBeInTheDocument();          // an emptied field is not the default
    await user.type(workers, "4");
    await user.selectOptions(screen.getByLabelText("Acquisition mode"), "legacy");
    await user.click(screen.getByRole("switch", { name: "Include Level 3 open research" }));
    await user.click(screen.getByRole("button", { name: /Start research/ }));
    await waitFor(() => expect(lastStart(api.calls)).toBeDefined());
    expect(lastStart(api.calls)).toMatchObject({ profile: "custom",
                                                settings: { workers: 4, acquisition_mode: "legacy", include_level3: true } });
    expect(Object.keys(lastStart(api.calls).settings as object).sort()).toEqual(["acquisition_mode", "include_level3", "workers"]);
  });

  it("marks settings a named profile pins and lists the server-controlled ones", async () => {
    mockApi();
    const user = await openNewResearch();
    await user.click(await screen.findByRole("button", { name: /Per-run settings/ }));
    expect(screen.getAllByText("Pinned").length).toBeGreaterThan(5);         // production profile
    expect(screen.getByText(/Credential: GLM_API_KEY/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/API key/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/API base URL/)).not.toBeInTheDocument();
  });

  it("reuses one idempotency key across a network retry and uses a new one for the next submission", async () => {
    let attempt = 0;
    const api = mockApi({
      "POST /api/runs": () => (++attempt === 1 ? new TypeError("network down") : started("run-r")),
      "GET /api/runs/run-r": () => json(F.completedRun),
    });
    const user = await openNewResearch();
    await user.click(screen.getAllByRole("option")[0].querySelector("button")!);
    await user.click(screen.getByRole("button", { name: /Start research/ }));
    expect(await screen.findByText("The run was not started")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(api.callsTo("POST", "/api/runs")).toHaveLength(2));
    const [first, second] = api.callsTo("POST", "/api/runs").map((c) => (c.body as Record<string, string>).idempotency_key);
    expect(first).toBe(second);
  });

  it("offers to open the existing run on a 409 conflict", async () => {
    mockApi({ "POST /api/runs": () => apiError(409, "run_rejected", "A run for this target is already active.",
                                               { existing_run_id: "run-existing" }) });
    const user = await openNewResearch();
    await user.click(screen.getAllByRole("option")[0].querySelector("button")!);
    await user.click(screen.getByRole("button", { name: /Start research/ }));
    expect(await screen.findByText("A conflicting run is already active")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open that run" })).toHaveAttribute("href", "/runs/run-existing");
  });

  it("blocks a target that already has an active run and links to it", async () => {
    mockApi();
    const user = await openNewResearch();
    await user.click(screen.getAllByRole("option")[2].querySelector("button")!);     // 101122: the active fixture run
    expect(screen.getByRole("link", { name: "open it" })).toHaveAttribute("href", `/runs/${F.ACTIVE_ID}`);
    expect(screen.getByRole("button", { name: /Start research/ })).toBeDisabled();
  });

  it("blocks a start while the server is not configured", async () => {
    mockApi({ "GET /api/config/status": () => json({ ...F.config, configured: false, blocking: ["GLM"] }) });
    const user = await openNewResearch();
    await user.click(screen.getAllByRole("option")[0].querySelector("button")!);
    expect(screen.getByText(/not configured to start research: GLM/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Start research/ })).toBeDisabled();
  });
});

describe("per-run settings helpers", () => {
  it("diffs against the contract defaults and checks ranges", () => {
    const values = defaultsOf(F.contract);
    expect(overridesOf(F.contract, values)).toEqual({});
    expect(overridesOf(F.contract, { ...values, workers: 3, temperature: null })).toEqual({ workers: 3 });
    const workers = F.contract.settings.find((s) => s.name === "workers")!;
    expect(settingError(workers, 0)).toMatch(/Minimum/);
    expect(settingError(workers, 2.5)).toMatch(/whole/);
    expect(settingError(workers, 7)).toBeNull();
    const model = F.contract.settings.find((s) => s.name === "model_id")!;
    expect(settingError(model, "bad model")).not.toBeNull();
  });
});
