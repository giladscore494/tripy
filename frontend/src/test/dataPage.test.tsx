// identity anchors PR: the Data page (open datasets + the production source policy and its logged switch).
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { apiError, json, mockApi, renderApp } from "./mockApi";

const datasets = {
  repo_dir: "/app/data/open", manifest_built_at: "2026-10-06T10:00:00+00:00",
  run_url: "https://github.com/giladscore494/tripy/actions/runs/1", config_version: "open-datasets-v3",
  datasets: [
    { dataset: "eea_co2_cars", label: "EEA CO2 emissions from new passenger cars (monitoring)",
      source_url: "https://discodata.eea.europa.eu/sql", market: "EU", routes: ["european", "unknown"],
      identity_only: false, policy: "allowed", licence: "CC-BY-4.0",
      attribution: "European Environment Agency / DG CLIMA, CO2 emissions from new passenger cars, {year}; filtered and aggregated by TRIPY",
      available: true, problem: null, file: "eea_co2_cars.sqlite.gz", bytes: 12582912, rows: 81234,
      built_at: "2026-10-06T09:58:00+00:00", years: [{ year: 2018, status: "built" }, { year: 2019, status: "built" }],
      absent_columns: { "2018": ["electric_range_km"] }, last_attempt: { status: "built" } },
    { dataset: "ademe_car_labelling", label: "ADEME Car Labelling", source_url: null, market: "FR",
      routes: ["european"], identity_only: false, policy: "allowed", licence: "Licence Ouverte 2.0",
      attribution: "ADEME – Car Labelling, {date}; processed by TRIPY", available: true, problem: null, rows: 4021,
      column_units: { power_kw: { unit: "kW", basis: "schema_description" }, energy_wh_km: { unit: "unknown" },
                      energy_wh_km_max: { unit: "unknown", unit_of: "energy_wh_km" } } },
    { dataset: "tc_cvs", label: "Transport Canada Canadian Vehicle Specifications", source_url: null, market: "CA",
      routes: ["american"], identity_only: false, policy: "allowed", licence: "OGL-Canada",
      attribution: "Contains information licensed under the Open Government Licence – Canada.", available: false,
      problem: "no_manifest_entry", last_attempt: { status: "stopped", reason: "dictionary_mismatch" } },
  ],
};
const storage = {
  volume: { total: 5 * 1024 ** 3, used: 4.9 * 1024 ** 3, free: 100 * 1024 ** 2, path: "/data" },
  folders: [{ name: "runs/", path: "/data/runs", bytes: 2 * 1024 ** 3 }, { name: "derived/", path: "/data/derived", bytes: 2.5 * 1024 ** 3 }],
  min_free_bytes: 200 * 1024 ** 2, low: true, open_data_dir: "/data/derived/open",
  startup_cleanup: { removed: [".eea_co2_cars.sqlite.tmp"], bytes_freed: 300 * 1024 ** 2 },
};
const entry = (id: string, domains: string[], policy: string, extra: Record<string, unknown> = {}) => ({
  id, kind: "domain", domains, policy, licence: null, attribution: null, bulk_store: false, terms_clause: null,
  terms_clause_source: null, checked_at: null, ...extra });
const policy = { version: "source-policy-v1", unlisted: "blocked", overlay_path: "/data/derived/source_policy_overlay.json",
  entries: [entry("cartube.co.il", ["cartube.co.il"], "blocked", { licence: "terms prohibit robots / commercial use" }),
            entry("importer_consumer_sites", ["bmw.co.il", "toyota.co.il"], "blocked")],
  changes: [] };

describe("Data page", () => {
  it("lists the committed snapshots from the manifest, their licences and the policy table", async () => {
    mockApi({ "GET /api/data/datasets": () => json(datasets), "GET /api/data/policy": () => json(policy),
              "GET /api/data/storage": () => json(storage) });
    renderApp("/data");
    expect(await screen.findByRole("heading", { name: "Open data and source policy" })).toBeInTheDocument();
    expect((await screen.findAllByText("EEA CO2 emissions from new passenger cars (monitoring)")).length).toBeGreaterThan(0);
    expect(screen.getByText("2026-10-06T10:00:00+00:00")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /build-open-data/ })).toHaveAttribute("href", datasets.run_url);
    expect(screen.getByText("2018, 2019")).toBeInTheDocument();
    expect(screen.getByText(/absent: 2018: electric_range_km/)).toBeInTheDocument();
    expect(screen.getByText(/unit unknown \(no offers\): energy_wh_km$/)).toBeInTheDocument();
    expect(screen.getByText(/last attempt: stopped \(dictionary_mismatch\)/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Rebuild/ })).not.toBeInTheDocument();
    expect(screen.getByText(/Contains information licensed under the Open Government Licence/)).toBeInTheDocument();
    expect(await screen.findByText("cartube.co.il", { selector: "span" })).toBeInTheDocument();
    expect(screen.getByText(/Unlisted domains are blocked/)).toBeInTheDocument();
  });

  it("shows the volume and deletes the open-data files only after a confirmation", async () => {
    const api = mockApi({
      "GET /api/data/datasets": () => json(datasets), "GET /api/data/policy": () => json(policy),
      "GET /api/data/storage": () => json(storage),
      "POST /api/data/storage/delete-open-data": () => json({ deleted: true, bytes_freed: 2 * 1024 ** 3,
        path: "/data/derived/open", storage: { ...storage, low: false } }),
    });
    renderApp("/data");
    const user = userEvent.setup();
    expect(await screen.findByText("The data volume is almost full")).toBeInTheDocument();
    expect(screen.getByText("derived/")).toBeInTheDocument();
    expect(screen.getByText(/1 partial or unbuilt/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Delete open-data files" }));
    expect(api.callsTo("POST", "/api/data/storage/delete-open-data")).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Confirm delete" }));
    await waitFor(() => expect(api.callsTo("POST", "/api/data/storage/delete-open-data")).toHaveLength(1));
    expect(await screen.findByText(/Deleted \/data\/derived\/open \(2\.0 GB freed\)/)).toBeInTheDocument();
    expect(screen.queryByText("The data volume is almost full")).not.toBeInTheDocument();
  });

  it("switches a domain to allowed with its terms clause and shows the logged change", async () => {
    let posted: unknown = null;
    const api = mockApi({
      "GET /api/data/datasets": () => json(datasets), "GET /api/data/policy": () => json(policy),
      "GET /api/data/storage": () => json(storage),
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
              "GET /api/data/storage": () => json(storage),
              "POST /api/data/policy": () => apiError(422, "policy_change_refused",
                "allowing a domain needs the terms clause it rests on and the date it was checked") });
    renderApp("/data");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("Domain"), "example.co.il");
    await user.click(screen.getByRole("button", { name: "Save policy" }));
    const panel = await screen.findByText("The policy was not changed");
    expect(within(panel.closest("div") as HTMLElement).getByText(/needs the terms clause/)).toBeInTheDocument();
  });

  it("runs the shadow report and shows the agreement table, the proposed triples and the disagreements", async () => {
    const report = {
      version: "open-data-shadow-v1", generated_at: "2026-10-07T08:00:00+00:00", runs_dir: "/data/runs", min_agree: 5,
      snapshots_built_at: "2026-10-06T18:23:00+00:00", vehicles_total: 6, vehicles_failed: 0,
      table: [{ source: "eea_co2_cars", field: "curb_weight_kg", route: "european_co2", agree: 5, disagree: 0,
                offered_agree: 5, offered_disagree: 0, no_offer: 1 },
              { source: "eea_co2_cars", field: "wheelbase_mm", route: "european_co2", agree: 2, disagree: 1,
                offered_agree: 2, offered_disagree: 1, no_offer: 0 }],
      proposed: [{ source: "eea_co2_cars", field: "curb_weight_kg", route: "european_co2", agreements: 5 }],
      disagreements: [{ run_id: "20261006T184604Z", record_id: "22010", source: "eea_co2_cars", field: "wheelbase_mm",
                        route: "european_co2", status: "offered", offer_value: 2975, run_value: 2980, row_ids: [] }],
      type_code: {}, vehicles: [],
    };
    const api = mockApi({ "GET /api/data/datasets": () => json(datasets), "GET /api/data/policy": () => json(policy),
                          "GET /api/data/storage": () => json(storage),
                          "POST /api/data/open-data/shadow-report": () => json({ report }) });
    renderApp("/data");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Shadow report" }));
    await waitFor(() => expect(api.callsTo("POST", "/api/data/open-data/shadow-report")).toHaveLength(1));
    expect(await screen.findByText("(eea_co2_cars, curb_weight_kg, european_co2)")).toBeInTheDocument();
    expect(screen.getByText(/2975 vs run/)).toBeInTheDocument();
    expect(screen.getByText(/6 vehicle\(s\)/)).toBeInTheDocument();
  });

  it("reports a shadow report refused for lack of space", async () => {
    mockApi({ "GET /api/data/datasets": () => json(datasets), "GET /api/data/policy": () => json(policy),
              "GET /api/data/storage": () => json(storage),
              "POST /api/data/open-data/shadow-report": () => apiError(507, "insufficient_disk",
                "open-data shadow report refused: 100 MB free") });
    renderApp("/data");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "Shadow report" }));
    expect(await screen.findByText("The shadow report did not run")).toBeInTheDocument();
  });
});
