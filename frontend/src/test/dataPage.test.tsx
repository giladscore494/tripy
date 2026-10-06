// identity anchors PR: the Data page (open datasets + the production source policy and its logged switch).
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { apiError, json, mockApi, renderApp } from "./mockApi";

const datasets = {
  folder: "/data/derived/open", building: null, interval_days: 30,
  datasets: [
    { dataset: "eea_co2_cars", label: "EEA CO2 emissions from new passenger cars (monitoring)",
      source_url: "https://discodata.eea.europa.eu/sql", market: "EU", routes: ["european", "unknown"],
      identity_only: false, policy: "allowed", licence: "CC-BY-4.0",
      attribution: "European Environment Agency / DG CLIMA, CO2 emissions from new passenger cars, {year}; filtered and aggregated by TRIPY",
      schema_verified: false, snapshot_built_at: "2026-10-06T03:00:00+00:00", snapshot_rows: 812345,
      last_build: { status: "built", rows: 812345 } },
    { dataset: "tc_cvs", label: "Transport Canada Canadian Vehicle Specifications", source_url: null, market: "CA",
      routes: ["american"], identity_only: false, policy: "allowed", licence: "OGL-Canada",
      attribution: "Contains information licensed under the Open Government Licence – Canada.", schema_verified: false,
      snapshot_built_at: null, snapshot_rows: null, last_build: { status: "stopped", reason: "no_download_url" } },
  ],
};
const entry = (id: string, domains: string[], policy: string, extra: Record<string, unknown> = {}) => ({
  id, kind: "domain", domains, policy, licence: null, attribution: null, bulk_store: false, terms_clause: null,
  terms_clause_source: null, checked_at: null, ...extra });
const policy = { version: "source-policy-v1", unlisted: "blocked", overlay_path: "/data/derived/source_policy_overlay.json",
  entries: [entry("cartube.co.il", ["cartube.co.il"], "blocked", { licence: "terms prohibit robots / commercial use" }),
            entry("importer_consumer_sites", ["bmw.co.il", "toyota.co.il"], "blocked")],
  changes: [] };

describe("Data page", () => {
  it("lists the snapshots, their licences and the policy table", async () => {
    mockApi({ "GET /api/data/datasets": () => json(datasets), "GET /api/data/policy": () => json(policy) });
    renderApp("/data");
    expect(await screen.findByRole("heading", { name: "Open data and source policy" })).toBeInTheDocument();
    expect((await screen.findAllByText("EEA CO2 emissions from new passenger cars (monitoring)")).length).toBeGreaterThan(0);
    expect(screen.getByText(/stopped: no_download_url/)).toBeInTheDocument();
    expect(screen.getByText(/Contains information licensed under the Open Government Licence/)).toBeInTheDocument();
    expect(await screen.findByText("cartube.co.il", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByText(/Unlisted domains are blocked/)).toBeInTheDocument();
  });

  it("switches a domain to allowed with its terms clause and shows the logged change", async () => {
    let posted: unknown = null;
    const api = mockApi({
      "GET /api/data/datasets": () => json(datasets), "GET /api/data/policy": () => json(policy),
      "POST /api/data/policy": (call) => {
        posted = call.body;
        return json({ change: { at: "2026-10-06T04:00:00+00:00", domain: "bmw.co.il", from: "blocked", to: "allowed",
                                terms_clause: "Content may be used for comparison services.", checked_at: "2026-10-06",
                                changed_by: "operator (Data page)" }, policy });
      },
    });
    renderApp("/data");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "bmw.co.il" }));
    expect(screen.getByLabelText("Domain")).toHaveValue("bmw.co.il");
    await user.type(screen.getByLabelText("Terms clause the decision rests on"), "Content may be used for comparison services.");
    await user.click(screen.getByRole("button", { name: "Save policy" }));
    await waitFor(() => expect(api.callsTo("POST", "/api/data/policy")).toHaveLength(1));
    expect(posted).toMatchObject({ domain: "bmw.co.il", policy: "allowed",
                                   terms_clause: "Content may be used for comparison services." });
    expect(await screen.findByText("bmw.co.il: blocked → allowed")).toBeInTheDocument();
  });

  it("reports a refused change", async () => {
    mockApi({ "GET /api/data/datasets": () => json(datasets), "GET /api/data/policy": () => json(policy),
              "POST /api/data/policy": () => apiError(422, "policy_change_refused",
                "allowing a domain needs the terms clause it rests on and the date it was checked") });
    renderApp("/data");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Domain"), "example.co.il");
    await user.click(screen.getByRole("button", { name: "Save policy" }));
    const panel = await screen.findByText("The policy was not changed");
    expect(within(panel.closest("div") as HTMLElement).getByText(/needs the terms clause/)).toBeInTheDocument();
  });
});
