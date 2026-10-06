"""NHTSA vPIC partial-VIN decode (identity keys only; data/source_policy.json `identity_only`).

For the American route only, and only when the government type code (degem_nm) has the shape of a VIN descriptor
section (5 characters): VIN = the make's WMI (data/open_datasets.json `wmi_by_make`) + degem_nm + "*" (check digit) +
the model-year code. The decode's model, engine code, displacement, body and drive are identity keys: they can veto a
match (another displacement / drive / model), never yield a field value. On demand, cached in the document cache (kind
`vpic`); a failed or empty decode is recorded, never retried in the run. Runs only when an American snapshot is present
(the decode corroborates a dataset match; it is never a match by itself).
"""

from __future__ import annotations

import json
import re
from typing import Any

from . import datasets as ds

VPIC_TIMEOUT_S = 15.0


def partial_vin(fingerprint: dict, payload: dict | None) -> tuple[str | None, str]:
    """(VIN pattern or None, reason)."""
    payload = payload or {}
    ident = payload.get("identity") or {}
    config = ds.config()
    vds = str(fingerprint.get("type_code") or ident.get("model_code") or "").strip().upper()
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{5}", vds):
        return None, "type_code_not_vds"
    wmis = (config.get("wmi_by_make") or {}).get(str(ident.get("manufacturer") or "").strip()) or []
    if not wmis:
        return None, "no_wmi_for_make"
    year = str(ident.get("year") or fingerprint.get("year") or "")
    code = (config.get("vin_year_codes") or {}).get(year)
    if not code:
        return None, "no_year_code"
    return f"{wmis[0]}{vds}*{code}", "ok"


def decode(ctx, vin: str, year: Any) -> dict:
    """The decode's identity keys {model, engine_code, displacement_l, body, drive} (cached per URL)."""
    from ..source_authority import fetch_allowed

    dataset = ds.datasets().get("nhtsa_vpic") or {}
    url = str(dataset.get("source_url") or "").replace("{vin}", vin).replace("{year}", str(year))
    if not url or not fetch_allowed(url):
        return {"status": "policy_blocked", "url": url}
    cached = ctx.cache.lookup("vpic", url)
    if cached:
        body = ctx.cache.read_text(cached["document_id"])
    else:
        try:
            resp = ctx.session.get(url, timeout=(ctx.config.connect_timeout_s, VPIC_TIMEOUT_S))
            body = resp.text if resp.status_code == 200 else ""
        except Exception as exc:  # noqa: BLE001
            return {"status": "failed", "url": url, "error": type(exc).__name__}
        if body:
            ctx.cache.put("vpic", url, body.encode("utf-8"), {"status": 200, "final_url": url, "doc_type": "text",
                                                              "content_type": "application/json"}, body)
    try:
        row = (json.loads(body).get("Results") or [{}])[0]
    except (ValueError, AttributeError, IndexError):
        return {"status": "no_decode", "url": url}
    keys = dataset.get("identity_keys") or {}
    found = {k: str(row.get(v) or "").strip() for k, v in keys.items() if str(row.get(v) or "").strip()}
    return {"status": "decoded" if found else "no_decode", "url": url, "identity": found}


def vpic_check(ctx, fingerprint: dict, payload: dict | None, result: dict) -> dict:
    """The vPIC entry of an American-route match: {status, vin, identity, vetoes}."""
    sources = result.get("sources") or {}
    if not any((sources.get(s) or {}).get("status") not in (None, "no_snapshot")
               for s in ("epa_fueleconomy", "nrcan_fuel_ratings", "tc_cvs")):
        return {"source": "nhtsa_vpic", "status": "not_run", "reason": "no_american_snapshot"}
    vin, reason = partial_vin(fingerprint, payload)
    if vin is None:
        return {"source": "nhtsa_vpic", "status": "not_applicable", "reason": reason}
    year = ((payload or {}).get("identity") or {}).get("year")
    decoded = decode(ctx, vin, year)
    out = {"source": "nhtsa_vpic", "vin": vin, **decoded, "vetoes": []}
    found = decoded.get("identity") or {}
    keys = result.get("keys") or {}
    if found.get("displacement_l") and keys.get("cc"):
        try:
            if abs(float(found["displacement_l"]) * 1000 - float(keys["cc"])) > max(50.0, 0.01 * float(keys["cc"])):
                out["vetoes"].append("displacement")
        except ValueError:
            pass
    drive = str(found.get("drive") or "").lower()
    if drive and keys.get("drivetrain"):
        awd = any(t in drive for t in ("awd", "4wd", "all-wheel", "4-wheel", "4x4"))
        if awd != (keys["drivetrain"] == "awd"):
            out["vetoes"].append("drive")
    if found.get("model") and keys.get("models") and not any(
            m in found["model"].upper() for m in keys["models"]):
        out["vetoes"].append("model")
    return out
