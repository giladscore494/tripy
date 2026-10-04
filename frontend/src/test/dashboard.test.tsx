import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { mockApi, renderApp } from "./mockApi";
import * as F from "./fixtures";

describe("dashboard and run history", () => {
  it("shows active research, history and the server configuration from the API", async () => {
    mockApi();
    renderApp("/");
    const live = (await screen.findByRole("heading", { name: "Active research" })).closest("section")!;
    expect(await within(live).findByText(F.ACTIVE_ID)).toBeInTheDocument();
    expect(screen.getByText("Server configuration")).toBeInTheDocument();
    expect(screen.getAllByText(F.config.research_model!).length).toBeGreaterThan(0);
    const attention = screen.getByRole("heading", { name: "Failed or interrupted" }).closest("section")!;
    expect(within(attention).getByText(F.interruptedRun.run_id)).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: /New Research/ }).every((l) => l.getAttribute("href") === "/research/new")).toBe(true);
  });

  it("filters the run history by status and search", async () => {
    mockApi();
    renderApp("/runs");
    await screen.findByRole("heading", { name: "Runs" });
    const user = userEvent.setup();
    expect(await screen.findByText(F.MULTI_ID)).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /Interrupted/ }));
    expect(screen.getByText(F.interruptedRun.run_id)).toBeInTheDocument();
    expect(screen.queryByText(F.MULTI_ID)).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /^All/ }));
    await user.type(screen.getByLabelText("Search runs"), "manufacturer");
    expect(screen.getByText(F.MULTI_ID)).toBeInTheDocument();
    expect(screen.queryByText(F.ACTIVE_ID)).not.toBeInTheDocument();
    await user.clear(screen.getByLabelText("Search runs"));
    await user.type(screen.getByLabelText("Search runs"), "zzz-no-match");
    expect(screen.getByText("No run matches these filters")).toBeInTheDocument();
  });

  it("collapses navigation into a drawer on mobile", async () => {
    mockApi();
    renderApp("/");
    await screen.findByRole("heading", { name: "Research Intelligence Workspace" });
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Open navigation" }));
    const drawer = screen.getByRole("dialog", { name: "Navigation" });
    await user.click(within(drawer).getByRole("link", { name: "Diagnostics" }));
    expect(screen.queryByRole("dialog", { name: "Navigation" })).not.toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Diagnostics" })).toBeInTheDocument();
  });
});
