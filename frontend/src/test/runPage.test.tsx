import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PROGRESS_MS } from "../hooks/useRunMonitor";
import { json, mockApi, renderApp } from "./mockApi";
import * as F from "./fixtures";

const SYNTH = F.completedRun.run_id;

function runRoutes(detail = F.completedRun) {
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
    "GET /api/documents/*/text": () => json(F.documentText),
    "GET /api/documents/*/structure": () => json(F.documentStructure),
  };
}

afterEach(() => vi.useRealTimers());

describe("active run", () => {
  it("polls progress while active, shows live stages and stops once the run is terminal", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let detail = F.activeRun();
    const api = mockApi({
      ...runRoutes(detail),
      [`GET /api/runs/${F.ACTIVE_ID}`]: () => json(detail),
      [`GET /api/runs/${F.ACTIVE_ID}/progress`]: () => json(F.progressOf(detail)),
      [`GET /api/runs/${F.ACTIVE_ID}/events`]: (call) => json(F.eventsPage(Number(call.query.get("after")),
        Number(call.query.get("after")) === 0 ? [F.event(1, "run_started"), F.event(2, "model_response", { model: "glm-5.3-flash", phase: "research" })] : [])),
    });
    renderApp(`/runs/${F.ACTIVE_ID}`);
    expect(await screen.findByRole("button", { name: /Cancel run/ })).toBeInTheDocument();
    expect(screen.getAllByText("Source acquisition").length).toBeGreaterThan(0);
    await act(async () => { await vi.advanceTimersByTimeAsync(PROGRESS_MS * 3 + 100); });
    const polled = api.callsTo("GET", `/api/runs/${F.ACTIVE_ID}/progress`).length;
    expect(polled).toBeGreaterThanOrEqual(3);

    // the run completes: a status change refreshes the detail and progress polling stops
    detail = { ...F.completedRun, run_id: F.ACTIVE_ID };
    await act(async () => { await vi.advanceTimersByTimeAsync(PROGRESS_MS + 100); });
    await waitFor(() => expect(screen.queryByRole("button", { name: /Cancel run/ })).not.toBeInTheDocument());
    const after = api.callsTo("GET", `/api/runs/${F.ACTIVE_ID}/progress`).length;
    await act(async () => { await vi.advanceTimersByTimeAsync(PROGRESS_MS * 4); });
    expect(api.callsTo("GET", `/api/runs/${F.ACTIVE_ID}/progress`).length).toBe(after);
  });

  it("refreshes stale active detail when the first progress response is already terminal", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const active = F.activeRun();
    const done = { ...F.completedRun, run_id: F.ACTIVE_ID };
    let detailReads = 0;
    const api = mockApi({
      ...runRoutes(active),
      [`GET /api/runs/${F.ACTIVE_ID}`]: () => json(detailReads++ === 0 ? active : done),
      [`GET /api/runs/${F.ACTIVE_ID}/progress`]: () => json(F.progressOf(done)),
      [`GET /api/runs/${F.ACTIVE_ID}/events`]: () => json(F.eventsPage(0, [])),
    });

    renderApp(`/runs/${F.ACTIVE_ID}`);

    await waitFor(() => expect(api.callsTo("GET", `/api/runs/${F.ACTIVE_ID}`).length).toBeGreaterThanOrEqual(2));
    await waitFor(() => expect(screen.queryByRole("button", { name: /Cancel run/ })).not.toBeInTheDocument());

    const progressCalls = api.callsTo("GET", `/api/runs/${F.ACTIVE_ID}/progress`).length;
    await act(async () => { await vi.advanceTimersByTimeAsync(PROGRESS_MS * 4); });
    expect(api.callsTo("GET", `/api/runs/${F.ACTIVE_ID}/progress`).length).toBe(progressCalls);
  });

  it("cancels through the API", async () => {
    const api = mockApi({ ...runRoutes(F.activeRun()),
                          [`POST /api/runs/${F.ACTIVE_ID}/cancel`]: () => json({ run_id: F.ACTIVE_ID, status: "RESEARCHING",
                                                                                  message: "Cancellation requested" }, 202) });
    renderApp(`/runs/${F.ACTIVE_ID}`);
    await userEvent.setup().click(await screen.findByRole("button", { name: /Cancel run/ }));
    await waitFor(() => expect(api.callsTo("POST", `/api/runs/${F.ACTIVE_ID}/cancel`)).toHaveLength(1));
  });

  it("shows the live timeline from the event cursor", async () => {
    mockApi({ ...runRoutes(F.activeRun()),
              [`GET /api/runs/${F.ACTIVE_ID}/events`]: (call) => json(F.eventsPage(Number(call.query.get("after")),
                Number(call.query.get("after")) === 0 ? [F.event(1, "tool_call", { name: "web_search", arguments: { query: "G6 specs" }, phase: "research" }),
                                                         F.event(2, "document_sweep_failed", { error: "HTTP 500" })] : [])) });
    renderApp(`/runs/${F.ACTIVE_ID}?tab=timeline`);
    expect(await screen.findByText("web_search(G6 specs)")).toBeInTheDocument();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: /Problems/ }));
    expect(screen.queryByText("web_search(G6 specs)")).not.toBeInTheDocument();
    expect(screen.getByText("HTTP 500")).toBeInTheDocument();
  });
});

describe("multi-vehicle run", () => {
  it("lists every vehicle and re-targets the per-vehicle views without losing the run", async () => {
    const api = mockApi(runRoutes(F.multiRun));
    renderApp(`/runs/${F.MULTI_ID}?tab=candidates`);
    const list = await screen.findByRole("list", { name: "Vehicles of this run" });
    const cards = within(list).getAllByRole("button");
    expect(cards).toHaveLength(F.multiRun.vehicles.length);
    await waitFor(() => expect(api.callsTo("GET", `/api/runs/${F.MULTI_ID}/candidates`).length).toBeGreaterThan(0));
    const second = F.multiRun.vehicles[1].record_id;
    await userEvent.setup().click(cards[1]);
    await waitFor(() => expect(api.callsTo("GET", `/api/runs/${F.MULTI_ID}/candidates`)
      .some((c) => c.query.get("record_id") === second)).toBe(true));
    expect(api.callsTo("GET", `/api/runs/${F.MULTI_ID}/events`).some((c) => c.query.get("record_id") === second)).toBe(true);
    expect(screen.getByText(F.multiRun.label)).toBeInTheDocument();              // the parent run context stays
  });
});

describe("completed run views", () => {
  it("renders results as a readable field table, with raw JSON only as a technical expander", async () => {
    const id = F.results.run_id;
    mockApi({ ...runRoutes({ ...F.completedRun, run_id: id, vehicles: [{ ...F.completedRun.vehicles[0], record_id: F.results.vehicles[0].record_id, failure: null }] }) });
    renderApp(`/runs/${id}?tab=results`);
    expect(await screen.findByText(F.results.vehicles[0].fields[0].field)).toBeInTheDocument();
    expect(screen.getByText("Summary")).toBeInTheDocument();
    expect(screen.getByText("Raw result JSON (technical)")).toBeInTheDocument();
    expect(screen.queryByText(/"fields":/)).not.toBeInTheDocument();
  });

  it("filters candidates and shows evidence with safe external links", async () => {
    mockApi(runRoutes());
    const { unmount } = renderApp(`/runs/${SYNTH}?tab=candidates`);
    const field = F.candidates.fields.find((f) => f.candidates.length)!;
    expect(await screen.findByText(field.field)).toBeInTheDocument();
    await userEvent.setup().selectOptions(screen.getByLabelText("Candidate status"), "rejected");
    unmount();

    renderApp(`/runs/${SYNTH}?tab=evidence`);
    const first = F.evidence.admitted[0];
    expect((await screen.findAllByText(String(first.field))).length).toBeGreaterThan(0);
    const links = screen.getAllByRole("link").filter((a) => a.getAttribute("href")?.startsWith("https://"));
    expect(links.length).toBeGreaterThan(0);
    links.forEach((a) => {
      expect(a).toHaveAttribute("target", "_blank");
      expect(a).toHaveAttribute("rel", "noopener noreferrer");
    });
  });

  it("lists documents and opens their text and structure", async () => {
    const api = mockApi(runRoutes());
    renderApp(`/runs/${SYNTH}?tab=documents`);
    const doc = F.documents.documents[0];
    expect(await screen.findByText(doc.doc_id)).toBeInTheDocument();
    expect(screen.getAllByText(/HTTP 200/).length).toBeGreaterThan(0);
    const user = userEvent.setup();
    await user.click(screen.getAllByRole("button", { name: "Open" })[0]);
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(/XPENG G6/, { selector: "div" })).toBeInTheDocument();
    await user.click(within(dialog).getByRole("tab", { name: "Structure" }));
    expect(await within(dialog).findByText(/Tables \(/)).toBeInTheDocument();
    const structure = api.calls.find((c) => c.path.endsWith("/structure"))!;
    expect(structure.query.get("run_id")).toBe(SYNTH);
  });

  it("shows Binding Replay recorded vs now", async () => {
    mockApi(runRoutes());
    renderApp(`/runs/${SYNTH}?tab=replay`);
    expect(await screen.findByText("Fields ok (recorded → now)")).toBeInTheDocument();
    const vehicle = F.bindingReplay.summary.vehicle as Record<string, number>;
    expect(screen.getByText(`${vehicle.fields_ok_recorded} → ${vehicle.fields_ok_now}`)).toBeInTheDocument();
    expect(screen.getAllByText("Would be ok").length).toBeGreaterThan(0);
  });

  it("downloads canonical exports through the authenticated client", async () => {
    window.sessionStorage.setItem("tripy.access", "tok-123456");
    const api = mockApi({ ...runRoutes(),
      [`GET /api/runs/${SYNTH}/export/benchmark.json`]: () => ({ status: 200, text: "{}",
        headers: { "Content-Disposition": 'attachment; filename="tripy_benchmark.json"' } }) });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    renderApp(`/runs/${SYNTH}`);
    await userEvent.setup().click(await screen.findByRole("button", { name: "benchmark.json" }));
    await waitFor(() => expect(click).toHaveBeenCalled());
    const call = api.callsTo("GET", `/api/runs/${SYNTH}/export/benchmark.json`)[0];
    expect(call.headers.Authorization).toBe("Bearer tok-123456");
    expect(call.query.toString()).not.toContain("tok-123456");
    expect(URL.createObjectURL).toHaveBeenCalled();
  });
});

describe("failure experience", () => {
  it("explains the failure and offers only the actions the backend allows", async () => {
    const run = F.interruptedRun;
    const api = mockApi({ ...runRoutes(run),
      [`POST /api/runs/${run.run_id}/vehicles/${run.vehicles[0].record_id}/restart`]: () =>
        json({ run_id: "run-new", created: true, message: "", warnings: [], run: null, settings_overridden: [] }, 201),
      "GET /api/runs/run-new": () => json({ ...F.activeRun(), run_id: "run-new" }) });
    renderApp(`/runs/${run.run_id}`);
    const failure = run.vehicles[0].failure!;
    expect(await screen.findByText(failure.title)).toBeInTheDocument();
    expect(screen.getByText(failure.reason)).toBeInTheDocument();
    expect(screen.getByText("What survived")).toBeInTheDocument();
    expect(failure.actions).toEqual(["restart"]);
    expect(screen.queryByRole("button", { name: /Finalize preserved research/ })).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole("button", { name: /Restart this vehicle/ }));
    await waitFor(() => expect(api.calls.some((c) => c.method === "POST" && c.path.endsWith("/restart"))).toBe(true));
    const body = api.calls.find((c) => c.path.endsWith("/restart"))!.body as Record<string, string>;
    expect(body.idempotency_key).toMatch(/^restart:/);
  });

  it("finalizes preserved research when the backend offers it", async () => {
    const run = F.completedRun;                      // the fixture's failure card offers finalize and restart
    const rid = run.vehicles[0].record_id;
    const api = mockApi({ ...runRoutes(run),
      [`POST /api/runs/${SYNTH}/vehicles/${rid}/finalize`]: () => json({ run_id: SYNTH, record_id: rid, status: "FINALIZING",
                                                                       message: "Finalization from the preserved research started." }, 202) });
    renderApp(`/runs/${SYNTH}`);
    await userEvent.setup().click(await screen.findByRole("button", { name: /Finalize preserved research/ }));
    expect(await screen.findByText("Finalization from the preserved research started.")).toBeInTheDocument();
    expect(api.callsTo("POST", `/api/runs/${SYNTH}/vehicles/${rid}/finalize`)).toHaveLength(1);
  });

  it("shows a clean not-found state for an unknown run", async () => {
    mockApi({ "GET /api/runs/nope": () => ({ status: 404, body: { error: { code: "unknown_run", message: "Unknown run: 'nope'." } } }) });
    renderApp("/runs/nope");
    expect(await screen.findByText("This run does not exist")).toBeInTheDocument();
  });
});
