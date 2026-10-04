import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SERIES_ACTIVE_MS } from "../pages/SeriesPages";
import { apiError, json, mockApi, renderApp } from "./mockApi";
import * as F from "./fixtures";

afterEach(() => vi.useRealTimers());

const startedSeries = (id: string) => json({ series_id: id, created: true, message: "", series: null }, 201);

describe("A/B series", () => {
  it("creates a series with the chosen vehicles, runs per arm and arms in ARMS order", async () => {
    const api = mockApi({ "POST /api/series": () => startedSeries("series-new"),
                          "GET /api/series/series-new": () => json(F.runningSeries()) });
    renderApp("/series");
    const user = userEvent.setup();
    const list = await screen.findByRole("list", { name: "Series vehicles" });
    await user.click(within(list).getAllByRole("checkbox")[0]);
    await user.click(within(list).getAllByRole("checkbox")[2]);
    const repeats = screen.getByLabelText("Runs per arm");
    await user.clear(repeats);
    await user.type(repeats, "9");
    expect(screen.getByRole("button", { name: /Start A\/B series/ })).toBeDisabled();   // runs per arm: 1-5
    await user.clear(repeats);
    await user.type(repeats, "2");
    // defaults: baseline + treatment; add treatment + card, then click baseline off and on again (order must not matter)
    await user.click(screen.getByRole("checkbox", { name: "Benchmark: Treatment + card" }));
    await user.click(screen.getByRole("checkbox", { name: "Benchmark: Baseline" }));
    await user.click(screen.getByRole("checkbox", { name: "Benchmark: Baseline" }));
    expect(screen.getByText("6 run(s)")).toBeInTheDocument();
    await user.dblClick(screen.getByRole("button", { name: /Start A\/B series/ }));
    await waitFor(() => expect(api.callsTo("POST", "/api/series").length).toBeGreaterThan(0));
    expect(api.callsTo("POST", "/api/series")).toHaveLength(1);    // no duplicate submission
    const body = api.callsTo("POST", "/api/series")[0].body as Record<string, unknown>;
    expect(body).toMatchObject({ record_ids: ["38626", "101122"], repeats: 2,
                                 arms: ["benchmark_baseline", "benchmark_treatment", "benchmark_treatment_card"] });
    expect(String(body.idempotency_key)).toMatch(/^series:/);
    expect(await screen.findByRole("button", { name: /Cancel series/ })).toBeInTheDocument();
  });

  it("refuses a second series while one is running and links to it", async () => {
    mockApi({ "GET /api/series": () => json(F.seriesList({ executing: "series-running-1" })) });
    renderApp("/series");
    expect(await screen.findByText("A series is already running")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open it" })).toHaveAttribute("href", "/series/series-running-1");
    expect(screen.getByRole("button", { name: /Start A\/B series/ })).toBeDisabled();
  });

  it("maps a 409 from the server to the running series", async () => {
    mockApi({ "POST /api/series": () => apiError(409, "series_rejected", "An A/B series is already running (series-x).",
                                                 { existing_series_id: "series-x" }) });
    renderApp("/series");
    const user = userEvent.setup();
    const list = await screen.findByRole("list", { name: "Series vehicles" });
    await user.click(within(list).getAllByRole("checkbox")[0]);
    await user.click(screen.getByRole("button", { name: /Start A\/B series/ }));
    expect(await screen.findByRole("link", { name: "Open running series" })).toHaveAttribute("href", "/series/series-x");
  });

  it("shows live progress, cancels, and stops polling once the series is over", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let state = F.runningSeries();
    const api = mockApi({
      "GET /api/series/series-running-1": () => json(state),
      "POST /api/series/series-running-1/cancel": () => {
        state = { ...state, status: "CANCELLED", executing: false,
                  planned: state.planned.map((p, i) => ({ ...p, status: i === 0 ? "CANCELLED" : "CANCELLED" })),
                  series_progress: { ...state.series_progress, total: { planned: 2, done: 1, running: 0, not_run: 1 } } };
        return json(state, 202);
      },
    });
    renderApp("/series/series-running-1");
    expect(await screen.findByText("0 / 2 done · 1 running")).toBeInTheDocument();
    const planned = screen.getByRole("list", { name: "Planned runs" });
    expect(within(planned).getAllByRole("listitem")).toHaveLength(2);
    expect(within(planned).getByRole("link", { name: state.planned[0].run_id! })).toBeInTheDocument();
    await userEvent.setup({ advanceTimers: vi.advanceTimersByTime }).click(screen.getByRole("button", { name: /Cancel series/ }));
    await waitFor(() => expect(screen.getByText("1 / 2 done · 1 not run")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /Cancel series/ })).not.toBeInTheDocument();
    const polls = api.callsTo("GET", "/api/series/series-running-1").length;
    await act(async () => { await vi.advanceTimersByTimeAsync(SERIES_ACTIVE_MS * 4); });
    expect(api.callsTo("GET", "/api/series/series-running-1").length).toBe(polls);
  });

  it("offers the benchmark files of a completed series as authenticated downloads", async () => {
    const done = F.completedSeries;
    const api = mockApi({ [`GET /api/series/${done.series_id}`]: () => json(done),
                          [`GET /api/series/${done.series_id}/export/benchmark.json`]: () => ({ status: 200, text: "{}" }) });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    renderApp(`/series/${done.series_id}`);
    expect(await screen.findByText("2 / 2 done")).toBeInTheDocument();
    for (const name of done.benchmark!.files!) expect(screen.getByRole("button", { name })).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: "benchmark.json" }));
    await waitFor(() => expect(click).toHaveBeenCalled());
    expect(api.callsTo("GET", `/api/series/${done.series_id}/export/benchmark.json`)).toHaveLength(1);
  });
});

describe("diagnostics and settings", () => {
  it("shows server state without secrets and a run's backend diagnostics", async () => {
    const api = mockApi({ [`GET /api/runs/${F.completedRun.run_id}/diagnostics`]: () => json(F.diagnostics) });
    renderApp("/diagnostics");
    expect(await screen.findByText("Health and environment")).toBeInTheDocument();
    expect(await screen.findByText(F.config.prompt_version)).toBeInTheDocument();
    expect(screen.getByText(/not required · token/)).toBeInTheDocument();
    await userEvent.setup().selectOptions(screen.getByLabelText("Run for diagnostics"), F.completedRun.run_id);
    expect(await screen.findByText("Aggregate (benchmark.json content)")).toBeInTheDocument();
    expect(api.callsTo("GET", `/api/runs/${F.completedRun.run_id}/diagnostics`)).toHaveLength(1);
  });

  it("separates read-only server configuration from the per-run settings contract", async () => {
    mockApi();
    renderApp("/settings");
    expect(await screen.findByText("Nothing on this page changes the server")).toBeInTheDocument();
    expect(await screen.findByText("Per-run settings contract")).toBeInTheDocument();
    expect(screen.getAllByText("pinned").length).toBeGreaterThan(5);
    expect(screen.getAllByText(/Process-wide: RunManager.start/).length).toBe(2);   // chat and search limits
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();          // nothing editable here
  });
});
