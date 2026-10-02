"""Cross-run research memory: verified fact reuse, negative route memory and historical recovery yield.

Everything lives under `<document cache root>/memory/` as ONE FILE PER RECORD (facts) or PER RUN (routes, yield),
written atomically, so 50 concurrent vehicle workers never append to a shared file. Reads aggregate the files.

    VERIFIED FACTS   an admitted evidence item becomes reusable for other variants only when the field's schema
                     `reuse_scope` allows it (none | exact_market_trim | exact_technical_variant | body_powertrain),
                     the item bound to its own target at (at least) that level with variant_match=exact, and the
                     target's identity at that level is complete. Time-sensitive fields are never reused. A record
                     carries its schema identity (the field's meaning, units, policy) and the admission / binding /
                     harvester versions; a record from another identity is stale and ignored.
                     Reuse is NOT trust: a reused fact is re-admitted against the NEW target with the same gate
                     (src/evidence_admission.admit: the document is in the shared cache, the quote states the value,
                     server-side binding to the new target) and must bind at the reuse level again. It keeps its
                     original source identity (`reused_from`); the same fact reused on twenty variants is still ONE
                     source.
    NEGATIVE ROUTES  "this exact route (query / URL) already failed to give new usable material for this field in this
                     identity scope". Scheduling only: it never creates evidence, never changes a field state, never
                     marks a field not_applicable and never resolves a conflict. It only steers recovery away from
                     equivalent wasted operations, and it expires.
    RECOVERY YIELD   per-run tail statistics (attempts, turns, searches, documents, resolutions, by-product
                     resolutions, cost) by field, cluster, manufacturer, propulsion and source family. Used only to
                     ORDER recovery work, only with enough samples, and never as a confidence in any value.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .document_binding import BINDING_VERSION, LEVELS, TargetIdentity
from .fields import normalize_field_name
from .storage.atomic import atomic_write_json

MEMORY_VERSION = "memory-v1"
REUSE_LEVELS = ("exact_market_trim", "exact_technical_variant", "body_powertrain")
# identity parts each reuse level needs (all present, else there is no safe scope key and nothing is reused)
REQUIRED_PARTS = {"body_powertrain": ("manufacturer", "family", "year", "body", "propulsion"),
                  "exact_technical_variant": ("manufacturer", "family", "year", "body", "propulsion",
                                              "displacement_l", "power_hp", "drivetrain"),
                  "exact_market_trim": ("manufacturer", "family", "year", "body", "propulsion", "displacement_l",
                                        "power_hp", "drivetrain", "trim_tokens", "target_market")}
SPEC_IDENTITY_KEYS = ("name", "semantic_definition", "semantic_exclusions", "normalized_unit", "value_type",
                      "matcher", "binding_requirement", "reuse_scope", "applies_to", "not_applicable_when")
DEFAULT_FACT_MAX_AGE_DAYS = 365
DEFAULT_ROUTE_MAX_AGE_DAYS = 30
MIN_YIELD_SAMPLES = 5


def _digest(value: Any, size: int = 20) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")) \
        .hexdigest()[:size]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _age_days(stamp: str | None) -> float:
    try:
        then = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return float("inf")
    return (datetime.now(timezone.utc) - then).total_seconds() / 86400


def scope_key(identity: TargetIdentity | None, level: str) -> str | None:
    """The target's scope key at a reuse level, or None when a needed identity part is unknown (a key built from
    missing parts would collide across variants). A battery-electric target has no displacement."""
    if identity is None or level not in REQUIRED_PARTS:
        return None
    data = identity.as_dict()
    needed = [p for p in REQUIRED_PARTS[level]
              if not (p == "displacement_l" and data.get("propulsion") == "battery_electric")]
    if any(data.get(p) in (None, "", []) for p in needed):
        return None
    return identity.scope_key(level)


def spec_identity(spec: dict) -> str:
    """What a reusable fact means: the field's semantic / unit / policy keys and the gate versions."""
    from .candidate_harvest import HARVESTER_VERSION
    from .evidence_admission import ADMISSION_VERSION

    return _digest([MEMORY_VERSION, ADMISSION_VERSION, BINDING_VERSION, HARVESTER_VERSION,
                    {k: spec.get(k) for k in SPEC_IDENTITY_KEYS}])


def reuse_level(spec: dict) -> str | None:
    """The field's reuse scope, or None (never reusable: no policy, `none`, or time-sensitive)."""
    level = spec.get("reuse_scope")
    if level not in REUSE_LEVELS or spec.get("time_sensitive"):
        return None
    return level


def _level_ok(item: dict, level: str) -> bool:
    binding = item.get("binding_level")
    return (str(item.get("variant_match") or "").lower() == "exact" and binding in LEVELS
            and LEVELS.index(binding) >= LEVELS.index(level))


class ResearchMemory:
    def __init__(self, root: Path | str, *, fact_max_age_days: float = DEFAULT_FACT_MAX_AGE_DAYS,
                 route_max_age_days: float = DEFAULT_ROUTE_MAX_AGE_DAYS):
        self.root = Path(root)
        self.fact_max_age_days = fact_max_age_days
        self.route_max_age_days = route_max_age_days

    @classmethod
    def for_cache(cls, cache) -> "ResearchMemory | None":
        root = getattr(cache, "root", None)
        if root is None:
            return None
        return cls(Path(root) / "memory",
                   fact_max_age_days=float(os.environ.get("FACT_REUSE_MAX_AGE_DAYS") or DEFAULT_FACT_MAX_AGE_DAYS),
                   route_max_age_days=float(os.environ.get("NEGATIVE_ROUTE_MAX_AGE_DAYS")
                                            or DEFAULT_ROUTE_MAX_AGE_DAYS))

    # --- verified facts ----------------------------------------------------------------------------------
    def _fact_dir(self, key: str, field: str) -> Path:
        return self.root / "facts" / _digest(key, 24) / normalize_field_name(field)

    def record_facts(self, evidence: Iterable[dict], specs: list[dict], identity: TargetIdentity | None,
                     origin: dict) -> list[dict]:
        """Write the run's reusable verified facts (one file per fact). Returns the records written."""
        by_name = {s["name"]: s for s in specs}
        written = []
        for item in evidence:
            name = normalize_field_name(item.get("field"))
            spec = by_name.get(name)
            if not spec or item.get("admission_status") != "accepted" or item.get("reused_from"):
                continue                          # a reused fact is already recorded under its original source
            level = reuse_level(spec)
            key = scope_key(identity, level) if level else None
            if not key or not _level_ok(item, level) or not item.get("document_id") or not item.get("quote"):
                continue
            identity_hash = spec_identity(spec)
            record = {
                "memory_version": MEMORY_VERSION, "field": name, "value": item.get("value"), "unit": item.get("unit"),
                "condition": item.get("condition"), "scope_type": level, "scope_key": key,
                "market": item.get("market"), "source_url": item.get("source_url"),
                "document_id": item.get("document_id"), "quote": item.get("quote"),
                "binding_level": item.get("binding_level"), "variant_match": item.get("variant_match"),
                "source_authority": item.get("source_authority"), "admission_passed": True,
                "spec_identity": identity_hash, "origin": {**origin, "evidence_id": item.get("evidence_id")},
                "recorded_at": _now(),
            }
            # one record per (fact, source): the same fact from the same source written by twenty variants is one
            # record, i.e. one source, never twenty
            record_id = _digest([name, key, json.dumps(item.get("value"), default=str), item.get("unit"),
                                 item.get("condition"), item.get("document_id"), item.get("quote"), identity_hash])
            path = self._fact_dir(key, name) / f"{record_id}.json"
            if path.exists():
                continue                          # first writer keeps the original origin
            record["record_id"] = record_id
            atomic_write_json(path, record)
            written.append(record)
        return written

    def reusable_facts(self, specs: list[dict], identity: TargetIdentity | None) -> tuple[list[dict], dict]:
        """(fresh records whose scope and schema identity match this target, skip counts by reason)."""
        out, skipped = [], {}
        for spec in specs:
            if not spec.get("applicable", True):
                continue
            level = reuse_level(spec)
            key = scope_key(identity, level) if level else None
            if not key:
                continue
            folder = self._fact_dir(key, spec["name"])
            if not folder.is_dir():
                continue
            wanted = spec_identity(spec)
            for path in sorted(folder.glob("*.json")):
                try:
                    record = json.loads(path.read_text("utf-8"))
                except (OSError, ValueError):
                    continue
                if record.get("scope_key") != key:           # a digest collision never leaks across scopes
                    reason = "scope_key_mismatch"
                elif record.get("spec_identity") != wanted:
                    reason = "stale_schema_or_gate_version"
                elif _age_days(record.get("recorded_at")) > self.fact_max_age_days:
                    reason = "expired"
                else:
                    out.append(record)
                    continue
                skipped[reason] = skipped.get(reason, 0) + 1
        return out, skipped

    # --- negative routes ------------------------------------------------------------------------------------
    def record_routes(self, entries: list[dict], run_key: str) -> None:
        """One file per run: [{scope_key, cluster, field, routes: [{signature, tool, route}], outcome}]."""
        if entries:
            atomic_write_json(self.root / "routes" / f"{_digest(run_key, 24)}.json",
                              {"memory_version": MEMORY_VERSION, "run": run_key, "recorded_at": _now(),
                               "entries": entries})

    def negative_routes(self, keys_by_field: dict[str, str]) -> dict[str, dict]:
        """{field: {"routes": [...], "attempts": n}} of unexpired failures recorded for exactly these scope keys."""
        folder = self.root / "routes"
        out: dict[str, dict] = {}
        if not folder.is_dir() or not keys_by_field:
            return out
        for path in sorted(folder.glob("*.json")):
            try:
                data = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if _age_days(data.get("recorded_at")) > self.route_max_age_days:
                continue
            for entry in data.get("entries") or []:
                field = entry.get("field")
                if field in keys_by_field and entry.get("scope_key") == keys_by_field[field] \
                        and entry.get("outcome") == "no_new_material":
                    slot = out.setdefault(field, {"routes": [], "attempts": 0})
                    slot["attempts"] += 1
                    known = {r["signature"] for r in slot["routes"]}
                    slot["routes"] += [r for r in entry.get("routes") or [] if r.get("signature") not in known]
        return out

    # --- recovery yield ---------------------------------------------------------------------------------------
    def record_yield(self, rows: list[dict], run_key: str) -> None:
        if rows:
            atomic_write_json(self.root / "yield" / f"{_digest(run_key, 24)}.json",
                              {"memory_version": MEMORY_VERSION, "run": run_key, "recorded_at": _now(), "rows": rows})

    def cluster_yield(self, manufacturer: str | None, propulsion: str | None,
                      min_samples: int = MIN_YIELD_SAMPLES) -> dict[str, dict]:
        """{cluster: {samples, turns, resolutions, resolutions_per_turn}} with at least `min_samples` attempts for this
        manufacturer and propulsion. A scheduling statistic, never a confidence in any value."""
        folder = self.root / "yield"
        totals: dict[str, dict] = {}
        if not folder.is_dir():
            return {}
        for path in sorted(folder.glob("*.json")):
            try:
                rows = json.loads(path.read_text("utf-8")).get("rows") or []
            except (OSError, ValueError):
                continue
            for row in rows:
                if row.get("kind") != "cluster" or row.get("manufacturer") != manufacturer \
                        or row.get("propulsion") != propulsion:
                    continue
                slot = totals.setdefault(row.get("cluster"), {"samples": 0, "turns": 0, "resolutions": 0})
                slot["samples"] += 1
                slot["turns"] += int(row.get("turns") or 0)
                slot["resolutions"] += int(row.get("resolutions") or 0) + int(row.get("byproduct_resolutions") or 0)
        return {c: {**v, "resolutions_per_turn": round(v["resolutions"] / v["turns"], 3) if v["turns"] else 0.0}
                for c, v in totals.items() if v["samples"] >= min_samples}


def route_signature(tool: str, route: str) -> str:
    return _digest([tool, " ".join(str(route or "").lower().split())], 16)
