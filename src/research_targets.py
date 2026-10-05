"""Research targets: the benchmark vehicle catalog and the research scopes (framework-neutral).

The HTTP API (src/api) chooses what to research with these functions, so every run records the same vehicles, label
and scope shape in its run_state.json as the runs before it.

    One vehicle    one benchmark record id            label: "<manufacturer> <model> · <year> · <trim>"
    Manufacturer   every vehicle of one manufacturer  label: "<manufacturer> · <n> vehicles"
    All 50         the whole Benchmark v1 sample      label: "Benchmark v1 · <n> vehicles"
"""

from __future__ import annotations

from dataclasses import dataclass

from .benchmark import benchmark_vehicles, manufacturers, select_vehicles, vehicle_label

ONE, MANUFACTURER, ALL = "One vehicle", "Manufacturer", "All 50"
SCOPES = (ONE, MANUFACTURER, ALL)


def vehicle_title(v: dict) -> str:
    return " · ".join(str(x) for x in (f"{v['manufacturer']} {v['model']}", v.get("year"), v.get("trim")) if x)


@dataclass(frozen=True)
class VehicleCatalog:
    vehicles: list[dict]
    by_id: dict[str, dict]
    labels: dict[str, str]          # record id -> selector label (benchmark.vehicle_label)
    titles: dict[str, str]          # record id -> run / pipeline title (vehicle_title)

    @classmethod
    def load(cls) -> "VehicleCatalog":
        vehicles = benchmark_vehicles()
        return cls(vehicles=vehicles, by_id={v["upstream_record_id"]: v for v in vehicles},
                   labels={v["upstream_record_id"]: vehicle_label(v) for v in vehicles},
                   titles={v["upstream_record_id"]: vehicle_title(v) for v in vehicles})

    def manufacturers(self) -> list[str]:
        return manufacturers(self.vehicles)

    def title(self, record_id: str) -> str:
        return self.titles.get(record_id, record_id)


def research_target(catalog: VehicleCatalog, scope: str, value: str | None = None) -> tuple[list[dict], str]:
    """(vehicles, label) of a dashboard scope. `value`: the record id (One vehicle) or the manufacturer."""
    if scope == ONE:
        selection = select_vehicles(catalog.vehicles, "one", value)
        return selection, catalog.titles[value] if value in catalog.titles else str(value)
    if scope == MANUFACTURER:
        selection = select_vehicles(catalog.vehicles, "manufacturer", value)
        return selection, f"{value} · {len(selection)} vehicles"
    if scope == ALL:
        selection = select_vehicles(catalog.vehicles, "all")
        return selection, f"Benchmark v1 · {len(selection)} vehicles"
    raise ValueError(f"unknown scope {scope!r}")
