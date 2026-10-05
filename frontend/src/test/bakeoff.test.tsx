import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { json, mockApi, renderApp } from "./mockApi";

const options = {
  backends: [
    { name: "glm", label: "Z.ai search-prime", available: true, reason: "", key_env: "GLM_API_KEY" },
    { name: "serper", label: "Serper", available: true, reason: "", key_env: "SERPER_API_KEY" },
    { name: "gemini", label: "Gemini", available: false, reason: "GEMINI_API_KEY is not set", key_env: "GEMINI_API_KEY" },
  ],
  records: [{ record_id: "12949", label: "אאודי A6 2018" }, { record_id: "101122", label: "אקספנג G6 2026" }],
  defaults: { backends: ["glm", "serper"], record_ids: [], fetch_top: true },
  note: "A backend without its key is unavailable. Results never enter a run's evidence.",
};

const job = (status: string, executing: boolean) => ({
  bakeoff_id: "bakeoff-1", label: "Search bake-off · glm, serper", status, executing,
  config: { backends: ["glm", "serper"], records: [], fetch_top: 1, label: "" },
  created_at: "2026-10-05T19:00:00+00:00", started_at: "2026-10-05T19:00:00+00:00",
  finished_at: executing ? null : "2026-10-05T19:20:00+00:00", progress: { done: 50, total: 50 }, error: null,
});

const metrics = (backend: string, hit: number, accepted: number) => ({
  backend, queries: 250, errors: 0, results: 2000, full_path_ratio: backend === "glm" ? 0.42 : 0.99,
  on_site_ratio: backend === "glm" ? 0.1 : 1, version_url_hit: hit, version_url_hit_ratio: hit / 50,
  first_version_rank: 2, israeli_domain_share: 0.8, unique_urls: 900, candidates: hit, fetched: hit,
  accepted, rejected: hit - accepted, rejected_by_reason: {}, latency_p50_ms: 800, latency_p95_ms: 2400,
  usd: 2.5, usd_per_1000_queries: 10, usd_per_record: 0.05, accepted_per_usd: accepted / 2.5,
});

describe("search bake-off", () => {
  it("shows an unavailable backend disabled and starts with the chosen backends and fetch option", async () => {
    const api = mockApi({
      "GET /api/bakeoffs/options": () => json(options),
      "GET /api/bakeoffs": () => json({ bakeoffs: [] }),
      "POST /api/bakeoffs": () => json({ bakeoff_id: "bakeoff-1", bakeoff: job("RUNNING", true) }, 201),
      "GET /api/bakeoffs/bakeoff-1": () => json({ bakeoff: job("RUNNING", true), summary: null, records: [] }),
    });
    renderApp("/bakeoffs");
    const user = userEvent.setup();
    const gemini = await screen.findByRole("checkbox", { name: "Backend gemini" });
    expect(gemini).toBeDisabled();
    expect(screen.getByText(/unavailable: GEMINI_API_KEY is not set/)).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Backend glm" })).toBeChecked();
    await user.click(screen.getByRole("checkbox", { name: "Fetch top candidate" }));
    await user.click(screen.getByRole("button", { name: /Start bake-off/ }));
    await waitFor(() => expect(api.callsTo("POST", "/api/bakeoffs")).toHaveLength(1));
    expect(api.callsTo("POST", "/api/bakeoffs")[0].body).toEqual({ backends: ["glm", "serper"], fetch_top: false });
    expect(await screen.findByRole("button", { name: /Cancel bake-off/ })).toBeInTheDocument();
  });

  it("renders the metrics table and the per-record rows of a finished bake-off", async () => {
    mockApi({
      "GET /api/bakeoffs/bakeoff-1": () => json({
        bakeoff: job("COMPLETED", false),
        summary: { schema: "tripy-search-bakeoff/1", status: "completed", records: 50, queries: 500,
                   metrics: [metrics("glm", 0, 0), metrics("serper", 41, 33)], note: "" },
        records: [{ record_id: "12949", vehicle: "אאודי A6 2018", backend: "serper", queries: 5, results: 50,
                    version_url_hit: true, first_version_rank: 1, candidate_url: "https://www.auto.co.il/cars/audi/a6/2018/1/",
                    candidate_score: 3, verdict: "accepted", verdict_reason: "power_match", usd: 0.005 }],
      }),
    });
    renderApp("/bakeoffs/bakeoff-1");
    const table = await screen.findByRole("table", { name: "Bake-off metrics" });
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(3);
    expect(within(rows[2]).getByText("serper")).toBeInTheDocument();
    expect(within(rows[2]).getByText("33")).toBeInTheDocument();          // accepted pages
    expect(within(rows[1]).getByText("42.0%")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /metrics\.csv/ })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Bake-off records" })).toBeInTheDocument();
  });
});
