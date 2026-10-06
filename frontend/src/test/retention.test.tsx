// Data-volume retention: delete / keep a run from the run view, the disk refusal's link to Data → Storage, and the
// Storage section's compaction preview and confirmation.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { apiError, json, mockApi, renderApp } from "./mockApi";
import * as F from "./fixtures";

const run = { ...F.completedRun, size_bytes: 12 * 1024 * 1024, pinned: false, compacted_at: null };

function runRoutes(detail: typeof run) {
  const id = detail.run_id;
  return {
    [`GET /api/runs/${id}`]: () => json(detail),
    [`GET /api/runs/${id}/progress`]: () => json(F.progressOf(detail)),
    [`GET /api/runs/${id}/events`]: () => json(F.eventsPage(0, [])),
    [`GET /api/runs/${id}/results`]: () => json(F.results),
    [`GET /api/runs/${id}/candidates`]: () => json(F.candidates),
    [`GET /api/runs/${id}/evidence`]: () => json(F.evidence),
    [`GET /api/runs/${id}/documents`]: () => json(F.documents),
    [`GET /api/runs/${id}/binding-replay`]: () => json(F.bindingReplay),
    [`GET /api/runs/${id}/diagnostics`]: () => json(F.diagnostics),
  };
}

describe("run retention in the run view", () => {
  it("shows the size, marks the run keep and deletes it only after the inline confirmation", async () => {
    const api = mockApi({
      ...runRoutes(run),
      [`POST /api/runs/${run.run_id}/keep`]: () => json({ run_id: run.run_id, pinned: true }),
      [`DELETE /api/runs/${run.run_id}`]: () => json({ run_id: run.run_id, deleted: true, bytes_freed: run.size_bytes }),
    });
    renderApp(`/runs/${run.run_id}`);
    const user = userEvent.setup();
    expect(await screen.findByText("12.0 MB on disk")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Keep" }));
    await waitFor(() => expect(api.callsTo("POST", `/api/runs/${run.run_id}/keep`)).toHaveLength(1));
    expect(api.callsTo("POST", `/api/runs/${run.run_id}/keep`)[0].body).toEqual({ keep: true });
    await user.click(screen.getByRole("button", { name: "Delete run" }));
    expect(api.callsTo("DELETE", `/api/runs/${run.run_id}`)).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Confirm delete" }));
    await waitFor(() => expect(api.callsTo("DELETE", `/api/runs/${run.run_id}`)).toHaveLength(1));
  });

  it("disables delete while the run is executing and shows the server's refusal", async () => {
    const executing = { ...run, executing: true };
    mockApi({ ...runRoutes(executing),
              [`DELETE /api/runs/${run.run_id}`]: () => apiError(409, "run_executing", "The run is executing.") });
    renderApp(`/runs/${run.run_id}`);
    expect(await screen.findByRole("button", { name: "Delete run" })).toBeDisabled();
  });
});

describe("the disk refusal", () => {
  it("links the refused start to Data → Storage with what compaction would free", async () => {
    mockApi({ "POST /api/runs": () => apiError(507, "insufficient_disk",
      "run start: only 33 MB free on the data volume (at least 200 MB needed); nothing was written. Compacting the old runs would free 180 MB. See Data → Storage.",
      { compaction_bytes: 180 * 1024 * 1024, storage_path: "/data#storage" }) });
    renderApp("/research/new");
    const user = userEvent.setup();
    const start = await screen.findByRole("button", { name: /Start research/ });
    await user.type(await screen.findByLabelText("Search vehicles"), "CORE");
    await user.click(within(within(screen.getByRole("listbox", { name: "Vehicles" })).getAllByRole("option")[0])
      .getByRole("button"));
    await user.click(start);
    expect(await screen.findByText("Not enough free space on the data volume")).toBeInTheDocument();
    expect(screen.getByText(/would free 180 MB/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open Data → Storage/ })).toHaveAttribute("href", "/data#storage");
  });
});

describe("the Storage section's retention", () => {
  it("shows what compaction frees before confirming it and the cache cleanup", async () => {
    const overview = {
      settings: { keep_newest: 15, pinned: ["run-a"] },
      compaction: { runs: [{ run_id: "old-1", bytes: 150 * 1024 * 1024, paths: 3, compacted_before: false }],
                    bytes: 150 * 1024 * 1024, kept: ["run-a"], busy: [] },
      cache: { documents: 40, bytes: 30 * 1024 * 1024, cache_bytes: 80 * 1024 * 1024, documents_bytes: 70 * 1024 * 1024,
               used_by_kept_runs: 12 },
      logs: { bytes: 6 * 1024 * 1024, cap_bytes: 20 * 1024 * 1024 }, history: [],
    };
    const storage = { volume: { total: 433 * 1024 ** 2, used: 400 * 1024 ** 2, free: 33 * 1024 ** 2, path: "/data" },
                      folders: [{ name: "runs/", path: "/data/runs", bytes: 235 * 1024 ** 2 }], min_free_bytes: 200 * 1024 ** 2,
                      low: true, open_data_dir: "/data/derived/open" };
    const api = mockApi({
      "GET /api/data/datasets": () => json({ repo_dir: "/app/data/open", manifest_built_at: null, run_url: null,
                                             config_version: "v3", datasets: [] }),
      "GET /api/data/policy": () => json({ version: "v1", unlisted: "blocked", entries: [], changes: [] }),
      "GET /api/data/storage": () => json(storage),
      "GET /api/data/retention": () => json(overview),
      "POST /api/data/retention/compact": () => json({ runs: [{ run_id: "old-1", bytes_freed: 150 * 1024 * 1024 }],
                                                       bytes_freed: 150 * 1024 * 1024, storage }),
    });
    renderApp("/data#storage");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Compact old runs now (150.0 MB)" }));
    expect(screen.getByText(/Compact 1 run\(s\) and free 150\.0 MB/)).toBeInTheDocument();
    expect(api.callsTo("POST", "/api/data/retention/compact")).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Confirm compact" }));
    await waitFor(() => expect(api.callsTo("POST", "/api/data/retention/compact")).toHaveLength(1));
    expect(await screen.findByText("Compacted 1 run(s): 150.0 MB freed.")).toBeInTheDocument();
    expect(screen.getByText(/40 document\(s\) \(30\.0 MB\)/)).toBeInTheDocument();
    expect(screen.getByText(/Logs: 6\.0 MB of a 20\.0 MB cap/)).toBeInTheDocument();
  });
});
