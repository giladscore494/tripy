// The views that replaced the last Streamlit-only screens: A/B series per-run settings, a vehicle's technical
// sections, the live field table, the Benchmark tab, the shared document cache, multi-run benchmark exports, batch
// comparison and GLM reachability. HTTP mocked with responses captured from the real backend.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { apiError, json, mockApi, renderApp } from "./mockApi";
import * as F from "./fixtures";

const SYNTH = F.completedRun.run_id;
const RECORD = F.completedRun.record_ids[0];

function runRoutes() {
  return {
    [`GET /api/runs/${SYNTH}`]: () => json(F.completedRun),
    [`GET /api/runs/${SYNTH}/progress`]: () => json(F.progressOf(F.completedRun)),
    [`GET /api/runs/${SYNTH}/events`]: () => json(F.eventsPage(0, [])),
    [`GET /api/runs/${SYNTH}/results`]: () => json(F.results),
    [`GET /api/runs/${SYNTH}/live`]: () => json(F.live),
    [`GET /api/runs/${SYNTH}/benchmark`]: () => json(F.benchmark),
    [`GET /api/runs/${SYNTH}/vehicles/${RECORD}/technical`]: () => json(F.technical),
    [`GET /api/runs/${SYNTH}/documents`]: () => json(F.documents),
    "GET /api/cache/documents": () => json({ total: 1, offset: 0, returned: 1, remaining: 0, next_offset: null,
                                             stats: { document_hits: 0 }, documents: [{ document_id: "d_cached_0001", kind: "html",
                                               url: "https://example.org/spec", status: 200, content_type: "text/html", text_chars: 1200 }] }),
  };
}

describe("A/B series settings", () => {
  it("sends the same typed per-run settings as a run, and none when untouched", async () => {
    const api = mockApi({ "POST /api/series": () => json({ series_id: "s-1", created: true, message: "", series: null }, 201),
                          "GET /api/series/s-1": () => json(F.runningSeries()) });
    renderApp("/series");
    const user = userEvent.setup();
    const list = await screen.findByRole("list", { name: "Series vehicles" });
    await user.click(within(list).getAllByRole("checkbox")[0]);
    await user.click(await screen.findByRole("button", { name: /Per-run settings for every run of the series/ }));
    expect(screen.getByText("the runs of this series only")).toBeInTheDocument();
    expect(screen.getAllByText("Pinned").length).toBeGreaterThan(5);       // each arm is a named profile
    const workers = screen.getByLabelText(/Vehicle workers/);
    await user.clear(workers);
    await user.type(workers, "99");
    expect(screen.getByRole("button", { name: /Start A\/B series/ })).toBeDisabled();   // validated like a run
    await user.clear(workers);
    await user.type(workers, "3");
    await user.selectOptions(screen.getByLabelText("Level 1.5 source"), "snapshot");
    await user.click(screen.getByRole("button", { name: /Start A\/B series/ }));
    await waitFor(() => expect(api.callsTo("POST", "/api/series")).toHaveLength(1));
    const body = api.callsTo("POST", "/api/series")[0].body as Record<string, unknown>;
    expect(body.settings).toEqual({ workers: 3, data_source: "snapshot" });
  });

  it("omits settings when the user changed none", async () => {
    const api = mockApi({ "POST /api/series": () => json({ series_id: "s-2", created: true, message: "", series: null }, 201),
                          "GET /api/series/s-2": () => json(F.runningSeries()) });
    renderApp("/series");
    const user = userEvent.setup();
    const list = await screen.findByRole("list", { name: "Series vehicles" });
    await user.click(within(list).getAllByRole("checkbox")[0]);
    await screen.findByRole("button", { name: /Per-run settings for every run of the series/ });
    await user.click(screen.getByRole("button", { name: /Start A\/B series/ }));
    await waitFor(() => expect(api.callsTo("POST", "/api/series")).toHaveLength(1));
    expect(api.callsTo("POST", "/api/series")[0].body).not.toHaveProperty("settings");
  });

  it("shows a 422 invalid_settings answer", async () => {
    mockApi({ "POST /api/series": () => apiError(422, "invalid_settings", "workers must be between 1 and 50") });
    renderApp("/series");
    const user = userEvent.setup();
    const list = await screen.findByRole("list", { name: "Series vehicles" });
    await user.click(within(list).getAllByRole("checkbox")[0]);
    await screen.findByRole("button", { name: /Per-run settings for every run of the series/ });
    await user.click(screen.getByRole("button", { name: /Start A\/B series/ }));
    expect(await screen.findByText("workers must be between 1 and 50")).toBeInTheDocument();
  });
});

describe("run views beyond the core workspace", () => {
  it("shows the live field table and the activity log on the pipeline tab", async () => {
    mockApi(runRoutes());
    renderApp(`/runs/${SYNTH}`);
    await userEvent.setup().click(await screen.findByRole("button", { name: /Live field progress and activity log/ }));
    const table = await screen.findByRole("table", { name: "Live field progress" });
    expect(within(table).getAllByRole("row")).toHaveLength(F.live.fields.length + 1);
    expect(screen.getByText(F.live.columns[0])).toBeInTheDocument();
    expect(screen.getByText(/Activity log/)).toBeInTheDocument();
  });

  it("renders a vehicle's technical sections from the backend", async () => {
    const api = mockApi(runRoutes());
    renderApp(`/runs/${SYNTH}?tab=technical`);
    expect(await screen.findByText(F.technical.no_output_message!, { exact: false })).toBeInTheDocument();
    expect(screen.getByText(F.technical.notices![0].text)).toBeInTheDocument();
    for (const title of ["Detailed diagnostics", "Partial research", "Cross-field consistency checks", "Tool calls",
                         "Run config & cost", "API attempts", "Field recovery", "Layered candidate metrics", "Level 1.5 input"]) {
      expect(screen.getByRole("button", { name: new RegExp(title) })).toBeInTheDocument();
    }
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /Partial research/ }));
    expect(screen.getByText(/Research material as collected/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Detailed diagnostics/ }));
    await user.click(screen.getByRole("tab", { name: "Acquisition turns" }));
    expect(screen.getByText("Run report")).toBeInTheDocument();
    for (const [label] of F.completedRun.status_panel ?? []) expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    expect(api.callsTo("GET", `/api/runs/${SYNTH}/vehicles/${RECORD}/technical`)).toHaveLength(1);
  });

  it("shows the Benchmark tab's observation metrics", async () => {
    mockApi(runRoutes());
    renderApp(`/runs/${SYNTH}?tab=benchmark`);
    expect(await screen.findByText(/Observation metrics only/)).toBeInTheDocument();
    expect(screen.getByText("Mean coverage")).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Per-vehicle observation metrics" });
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(F.benchmark.columns);
  });

  it("lists the entire document cache and opens a cached document", async () => {
    const api = mockApi({ ...runRoutes(), "GET /api/documents/*/text": () => json(F.documentText),
                          "GET /api/documents/*/structure": () => json(F.documentStructure) });
    renderApp(`/runs/${SYNTH}?tab=documents`);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("radio", { name: "Entire cache" }));
    await user.click(await screen.findByRole("button", { name: "d_cached_0001" }));
    await waitFor(() => expect(api.callsTo("GET", "/api/documents/d_cached_0001/text")).toHaveLength(1));
  });
});

describe("diagnostics page", () => {
  it("downloads benchmark files over the chosen runs and compares batches on demand", async () => {
    window.sessionStorage.setItem("tripy.access", "tok-123456");
    const api = mockApi({
      "GET /api/benchmark/summary": (call) => json({ run_ids: call.query.getAll("run_id"), vehicles: 3, mean_turns: 4.5,
                                                     mean_searches: 9, sweep_resolution_rate: 0.5,
                                                     files: ["benchmark.json", "per_vehicle.csv", "binding_replay_items.jsonl"] }),
      "GET /api/benchmark/export/per_vehicle.csv": () => ({ status: 200, text: "a,b\n",
        headers: { "Content-Disposition": 'attachment; filename="tripy_per_vehicle.csv"' } }),
      "GET /api/benchmark/batches": () => json({ batches: [{ batch: "b-1", vehicles: 2 }], columns: ["batch", "vehicles"] }),
      "GET /api/config/reachability": () => json({ checked: true, reachable: true, detail: "HTTP 404" }),
    });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    renderApp("/diagnostics");
    const user = userEvent.setup();
    expect(await screen.findByText(/3 vehicle run\(s\) · mean turns 4.5/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "parser_gaps.jsonl" })).toBeDisabled();      // the runs recorded none
    await user.click(screen.getByRole("button", { name: "per_vehicle.csv" }));
    await waitFor(() => expect(click).toHaveBeenCalled());
    const call = api.callsTo("GET", "/api/benchmark/export/per_vehicle.csv")[0];
    expect(call.query.getAll("run_id")).toEqual(F.runList.runs.slice(0, 15).map((r) => r.run_id));
    expect(call.headers.Authorization).toBe("Bearer tok-123456");
    expect(call.query.toString()).not.toContain("tok-123456");
    expect(api.callsTo("GET", "/api/benchmark/batches")).toHaveLength(0);              // only on demand
    await user.click(screen.getByRole("button", { name: "Compare batches" }));
    expect(await screen.findByRole("table", { name: "Batches" })).toBeInTheDocument();
    expect(await screen.findByText(/Connected · HTTP 404/)).toBeInTheDocument();
  });
});
