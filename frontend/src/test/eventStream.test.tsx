import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EVENT_POLL_MS, useEventStream } from "../hooks/useEventStream";
import { describe as describeEvent, categoryOf } from "../components/run/EventTimeline";
import { event, eventsPage } from "./fixtures";

describe("event cursor", () => {
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
  afterEach(() => vi.useRealTimers());

  it("starts at 0, follows next_cursor, fetches `more` pages immediately and appends without reloading", async () => {
    const pages: Record<number, ReturnType<typeof eventsPage>> = {
      0: eventsPage(0, [event(1, "run_started"), event(2, "tool_call", { name: "web_search" })], true),
      2: eventsPage(2, [event(3, "tool_result")], false),
      3: eventsPage(3, [event(4, "evidence", { evidence: { field: "torque_nm", value: 660, unit: "Nm" } })], false),
    };
    const fetchPage = vi.fn(async (after: number) => pages[after] ?? eventsPage(after, []));
    const { result } = renderHook(() => useEventStream("run-1", "101122", true, fetchPage));

    await waitFor(() => expect(result.current.events.map((e) => e.line)).toEqual([1, 2, 3]));
    expect(fetchPage.mock.calls.map((c) => c[0])).toEqual([0, 2]);       // `more` → next page at once, after=next_cursor
    expect(result.current.cursor).toBe(3);
    expect(result.current.caughtUp).toBe(true);

    await act(async () => { await vi.advanceTimersByTimeAsync(EVENT_POLL_MS + 50); });
    await waitFor(() => expect(result.current.events.map((e) => e.line)).toEqual([1, 2, 3, 4]));
    expect(fetchPage.mock.calls.map((c) => c[0])).toEqual([0, 2, 3]);    // only newer lines, never the history again
  });

  it("never appends a line twice and never uses the array length as the cursor", async () => {
    const fetchPage = vi.fn(async (after: number) => after === 0
      ? eventsPage(0, [event(5, "run_started"), event(9, "model_response")])             // filtered / skipped lines
      : eventsPage(after, [event(9, "model_response"), event(10, "run_finished")]));    // an overlapping line
    const { result } = renderHook(() => useEventStream("run-1", undefined, true, fetchPage));
    await waitFor(() => expect(result.current.cursor).toBe(9));
    await act(async () => { await vi.advanceTimersByTimeAsync(EVENT_POLL_MS + 50); });
    await waitFor(() => expect(result.current.events.map((e) => e.line)).toEqual([5, 9, 10]));
    expect(fetchPage.mock.calls[1][0]).toBe(9);
  });

  it("stops polling a terminal run once caught up", async () => {
    const fetchPage = vi.fn(async (after: number) => eventsPage(after, after === 0 ? [event(1, "run_finished")] : []));
    const { result } = renderHook(() => useEventStream("run-1", undefined, false, fetchPage));
    await waitFor(() => expect(result.current.caughtUp).toBe(true));
    await act(async () => { await vi.advanceTimersByTimeAsync(EVENT_POLL_MS * 6); });
    expect(fetchPage).toHaveBeenCalledTimes(1);
  });

  it("restarts from cursor 0 for another vehicle of the run", async () => {
    const fetchPage = vi.fn(async (after: number) => eventsPage(after, after === 0 ? [event(1, "run_started")] : []));
    const { result, rerender } = renderHook(({ rid }) => useEventStream("run-1", rid, false, fetchPage),
                                            { initialProps: { rid: "a" } });
    await waitFor(() => expect(result.current.events).toHaveLength(1));
    rerender({ rid: "b" });
    await waitFor(() => expect(fetchPage.mock.calls.filter((c) => c[0] === 0)).toHaveLength(2));
  });
});

describe("timeline descriptions", () => {
  it("are built from structured event fields", () => {
    expect(describeEvent(event(1, "evidence", { evidence: { field: "wheelbase_mm", value: 2890, unit: "mm",
                                                           source_url: "https://www.heyxpeng.co.il/g6" } })))
      .toMatchObject({ field: "wheelbase_mm", source: "heyxpeng.co.il", text: "wheelbase_mm = 2,890 mm" });
    expect(describeEvent(event(2, "tool_call", { name: "web_search", arguments: '{"query": "XPeng G6 מפרט"}' })).text)
      .toBe("web_search(XPeng G6 מפרט)");
    expect(categoryOf("document_sweep_failed")).toBe("problems");
    expect(categoryOf("tool_result")).toBe("tools");
    expect(categoryOf("candidates_harvested")).toBe("evidence");
    expect(categoryOf("field_recovery_started")).toBe("fields");
  });
});
