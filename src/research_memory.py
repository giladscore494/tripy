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
# identity parts each reuse level needs (all present, else there is no safe scope key and nothing is reused). The
# government model code separates variants the other parts cannot (battery size, transmission, FWD vs RWD at the
# same power); the exact market trim uses EVERY word of the government trim ("gr sport" != "sport", "7 seats").
REQUIRED_PARTS = {"body_powertrain": ("manufacturer", "family", "year", "body", "propulsion"),
                  "exact_technical_variant": ("manufacturer", "family", "year", "body", "propulsion",
                                              "displacement_l", "power_hp", "drivetrain", "model_code_tokens"),
                  "exact_market_trim": ("manufacturer", "family", "year", "body", "propulsion", "displacement_l",
                                        "power_hp", "drivetrain", "model_code_tokens", "trim_words",
                                        "target_market")}
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
    """Age in days; unparseable or future-dated stamps count as infinitely old (never trusted, never immortal)."""
    try:
        then = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return float("inf")
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    age = (datetime.now(timezone.utc) - then).total_seconds() / 86400
    return age if age >= -0.01 else float("inf")


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
    if level == "exact_market_trim":
        from .document_binding import vocabulary

        generic = {w.lower() for w in vocabulary().get("generic_trim_words") or []}
        if all(w in generic for w in data["trim_words"]):
            return None                    # a trim made only of generic words ("edition", "base") identifies nothing
    return level + ":" + json.dumps([data.get(p) for p in REQUIRED_PARTS[level]], ensure_ascii=False,
                                    sort_keys=True, default=str)


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


def _level_ok(item: dict, level: str | None) -> bool:
    binding = item.get("binding_level")
    return (level in LEVELS and str(item.get("variant_match") or "").lower() == "exact" and binding in LEVELS
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
        def days(name: str, default: float) -> float:
            try:
                return max(0.0, float(os.environ.get(name) or default))
            except ValueError:
                return default

        return cls(Path(root) / "memory", fact_max_age_days=days("FACT_REUSE_MAX_AGE_DAYS", DEFAULT_FACT_MAX_AGE_DAYS),
                   route_max_age_days=days("NEGATIVE_ROUTE_MAX_AGE_DAYS", DEFAULT_ROUTE_MAX_AGE_DAYS))

    # --- verified facts ----------------------------------------------------------------------------------
    def _fact_dir(self, key: str, field: str) -> Path:
        return self.root / "facts" / _digest(key, 24) / normalize_field_name(field)

    def record_facts(self, evidence: Iterable[dict], specs: list[dict], identity: TargetIdentity | None,
                     origin: dict, evaluation: list[dict] | None = None) -> list[dict]:
        """Write the run's reusable verified facts (one file per fact). Returns the records written.

        Only facts their OWN run settled are recorded: the field's final current_evaluation state is `ok`, it has no
        same-scope conflict and the item is not one of the conflicting ones. A fact its run left conflicting,
        unresolved or declared otherwise is never handed to a sibling as settled."""
        by_name = {s["name"]: s for s in specs}
        states = {e["field"]: e for e in evaluation or []}
        written = []
        for item in evidence:
            name = normalize_field_name(item.get("field"))
            spec = by_name.get(name)
            if not spec or item.get("admission_status") != "accepted" or item.get("reused_from"):
                continue                          # a reused fact is already recorded under its original source
            state = states.get(name)
            if state is None or state.get("state") != "ok" or state.get("conflict_evidence_ids") \
                    or str(item.get("evidence_id")) not in {str(i) for i in state.get("evidence_ids") or []}:
                continue
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
                "origin_field_state": state.get("state"),
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
                if not isinstance(record, dict):
                    continue
                if record.get("scope_key") != key or record.get("scope_type") != level:           # a digest collision never leaks across scopes
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

    def negative_routes(self, keys_by_field: dict[str, str],
                        identities: dict[str, str] | None = None) -> dict[str, dict]:
        """{field: {"routes": [...], "attempts": n}} of unexpired failures recorded for exactly these scope keys (and,
        when given, the same field identity: a parser or schema change clears them). Malformed files are skipped."""
        folder = self.root / "routes"
        out: dict[str, dict] = {}
        if not folder.is_dir() or not keys_by_field:
            return out
        for path in sorted(folder.glob("*.json")):
            try:
                data = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or _age_days(data.get("recorded_at")) > self.route_max_age_days:
                continue
            for entry in data.get("entries") or []:
                if not isinstance(entry, dict):
                    continue
                field = entry.get("field")
                if field not in keys_by_field or entry.get("scope_key") != keys_by_field[field] \
                        or entry.get("outcome") != "no_new_material":
                    continue
                if identities and entry.get("spec_identity") != identities.get(field):
                    continue
                slot = out.setdefault(field, {"routes": [], "attempts": 0})
                slot["attempts"] += 1
                known = {r.get("signature") for r in slot["routes"]}
                slot["routes"] += [r for r in entry.get("routes") or []
                                   if isinstance(r, dict) and r.get("signature") and r.get("signature") not in known]
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
                data = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            rows = data.get("rows") if isinstance(data, dict) else None
            for row in rows if isinstance(rows, list) else []:
                if not isinstance(row, dict) or row.get("kind") != "cluster" or row.get("manufacturer") != manufacturer \
                        or row.get("propulsion") != propulsion:
                    continue
                try:
                    turns = int(row.get("turns") or 0)
                    resolved = int(row.get("resolutions") or 0) + int(row.get("byproduct_resolutions") or 0)
                except (TypeError, ValueError):
                    continue
                slot = totals.setdefault(str(row.get("cluster")), {"samples": 0, "turns": 0, "resolutions": 0})
                slot["samples"] += 1
                slot["turns"] += turns
                slot["resolutions"] += resolved
        return {c: {**v, "resolutions_per_turn": round(v["resolutions"] / v["turns"], 3) if v["turns"] else 0.0}
                for c, v in totals.items() if v["samples"] >= min_samples}


def route_signature(tool: str, route: str) -> str:
    return _digest([tool, " ".join(str(route or "").lower().split())], 16)
