// PR #47 (B1 / B2): the live MILO catalog browser of New research and the derived trim index panel of Settings.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { apiError, json, mockApi, renderApp, type Call } from "./mockApi";
import * as F from "./fixtures";

const index = { available: true, building: false, path: "/data/derived/catalog_trim_index.json", interval_days: 7,
                status: "built", built_at: "2026-10-05T03:00:00+00:00", rows: 98930, entries: 61234, error: null };
const live = { mode: "live", browser_enabled: true, label: "live MILO catalog (public.catalog_variants_current)",
               max_set: 50, max_limit: 200, derived_index: index };
const snapshot = { mode: "snapshot", browser_enabled: false, label: "snapshot (50 benchmark records)", max_set: 50,
                   max_limit: 200, derived_index: { ...index, available: false, status: undefined, built_at: null } };
const item = (id: string, trim: string) => ({ record_id: id, manufacturer: "טויוטה", model: "COROLLA", year: 2024,
  trim, model_code: "ZWE211L", degem_cd: 139, body: "wagon", propulsion: "hybrid", drivetrain: "two_wheel_drive",
  power_hp: 98, engine_cc: 1798, label: `טויוטה COROLLA · 2024 · ${trim} (${id})` });

function liveRoutes() {
  return {
    "GET /api/catalog/status": () => json(live),
    "GET /api/catalog/manufacturers": () => json({ manufacturers: [{ manufacturer: "טויוטה", variants: 6729 }] }),
    "GET /api/catalog/models": () => json({ models: [{ model: "COROLLA", variants: 400 }] }),
    "GET /api/catalog/years": () => json({ years: [{ year: 2024, variants: 34 }] }),
    "GET /api/catalog/trims": () => json({ trims: [{ trim: "BUSINESS EDI", variants: 2 }] }),
    "GET /api/catalog": (call: Call) => json({ items: [item("38626", "BUSINESS EDI"), item("38635", "BUSINESS EDI")],
                                               total: 2, limit: Number(call.query.get("limit")), offset: 0,
                                               filters: Object.fromEntries(call.query.entries()) }),
  };
}

describe("live catalog", () => {
  it("disables the browser in snapshot mode", async () => {
    mockApi({ "GET /api/catalog/status": () => json(snapshot) });
    renderApp("/research/new");
    const user = userEvent.setup();
    await screen.findByRole("heading", { name: "New research" });
    await user.click(await screen.findByRole("radio", { name: /Live catalog/ }));
    expect(await screen.findByText(/Level 1.5 source: snapshot \(50 benchmark records\)/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Manufacturer")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Start research/ })).toBeDisabled();
  });

  it("filters with cascading pickers, lists the chosen set and starts it", async () => {
    const api = mockApi({ ...liveRoutes(), "POST /api/runs": () => json({ run_id: "run-c", created: true, message: "",
      warnings: [], run: null, settings_overridden: [] }, 201), "GET /api/runs/run-c": () => json(F.completedRun) });
    renderApp("/research/new");
    const user = userEvent.setup();
    await screen.findByRole("heading", { name: "New research" });
    await user.click(await screen.findByRole("radio", { name: /Live catalog/ }));
    await user.selectOptions(await screen.findByLabelText("Manufacturer"), "טויוטה");
    await user.selectOptions(await screen.findByLabelText("Model"), "COROLLA");
    await waitFor(() => expect(screen.getByLabelText("Model year")).not.toBeDisabled());
    await user.selectOptions(screen.getByLabelText("Model year"), "2024");
    const list = await screen.findByRole("listbox", { name: "Catalog vehicles" });
    const search = api.callsTo("GET", "/api/catalog").at(-1)!;
    expect(Object.fromEntries(search.query.entries())).toMatchObject({ manufacturer: "טויוטה", model: "COROLLA",
                                                                        year: "2024", limit: "50", offset: "0" });
    await user.click(within(within(list).getAllByRole("option")[0]).getByRole("button"));
    expect(within(screen.getByLabelText("Selected vehicles")).getByText(/38626/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Start research/ }));
    await waitFor(() => expect(api.callsTo("POST", "/api/runs")).toHaveLength(1));
    expect(api.callsTo("POST", "/api/runs")[0].body).toMatchObject({ scope: "set", record_ids: ["38626"] });
  });

  it("shows the derived index and rebuilds it now from Settings", async () => {
    const api = mockApi({ "GET /api/catalog/status": () => json(live),
                          "POST /api/catalog/index/rebuild": () => json({ started: true, status: index,
                                                                         message: "Rebuild started." }, 202) });
    renderApp("/settings");
    const user = userEvent.setup();
    expect(await screen.findByText("98930")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Rebuild now" }));
    expect(await screen.findByText("Rebuild started.")).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/catalog/index/rebuild")).toHaveLength(1);
  });

  it("reports a refused rebuild without a database", async () => {
    mockApi({ "GET /api/catalog/status": () => json(snapshot),
              "POST /api/catalog/index/rebuild": () => apiError(409, "catalog_unavailable", "DATABASE_URL is not set") });
    renderApp("/settings");
    expect(await screen.findByText("unavailable (no DATABASE_URL)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Rebuild now" })).toBeDisabled();
  });
});
