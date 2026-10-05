import { useMemo } from "react";

import type { RunSettingSpec, RunSettingsContract, RunSettingsOverrides, SettingValue } from "../../api/types";
import { Badge, Button, cx } from "../ui/primitives";

export type SettingsValues = Record<string, SettingValue>;

/** The current value (an emptied input is null, distinct from "never touched", which is the default). */
export function valueOf(values: SettingsValues, spec: RunSettingSpec): SettingValue {
  return spec.name in values ? values[spec.name] : spec.default;
}

export function defaultsOf(contract: RunSettingsContract): SettingsValues {
  return Object.fromEntries(contract.settings.map((s) => [s.name, s.default]));
}

/** Only the settings the user changed (the backend applies its own defaults to everything else). */
export function overridesOf(contract: RunSettingsContract, values: SettingsValues): RunSettingsOverrides {
  const out: RunSettingsOverrides = {};
  for (const spec of contract.settings) {
    const value = values[spec.name];
    if (value === undefined || value === spec.default) continue;
    if (value === null && !spec.nullable) continue;
    out[spec.name] = value;
  }
  return out;
}

/** Client-side check against the contract's own ranges / options (the server validates again, authoritatively). */
export function settingError(spec: RunSettingSpec, value: SettingValue): string | null {
  if (value === null || value === undefined) return spec.nullable || spec.default === null ? null : "Required";
  if (spec.kind === "int" || spec.kind === "float") {
    if (typeof value !== "number" || Number.isNaN(value)) return "Enter a number";
    if (spec.kind === "int" && !Number.isInteger(value)) return "Enter a whole number";
    if (spec.min !== null && value < spec.min) return `Minimum ${spec.min}`;
    if (spec.max !== null && value > spec.max) return `Maximum ${spec.max}`;
  }
  if (spec.kind === "choice" && !spec.options.includes(String(value))) return "Choose an option";
  if (spec.kind === "choice" && spec.unavailable_options?.[String(value)]) {
    return `Unavailable: ${spec.unavailable_options[String(value)]}`;
  }
  if (spec.kind === "model" || spec.kind === "text") {
    const text = String(value);
    if (!text && !spec.allow_empty) return "Required";
    const pattern = spec.kind === "model" ? /^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$/ : /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
    if (text && !pattern.test(text)) return "Letters, digits and . _ - only";
  }
  return null;
}

export function settingsErrors(contract: RunSettingsContract, values: SettingsValues): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const spec of contract.settings) {
    const error = settingError(spec, valueOf(values, spec));
    if (error) errors[spec.name] = error;
  }
  return errors;
}

function SettingInput({ spec, value, onChange, error }: {
  spec: RunSettingSpec; value: SettingValue; onChange: (value: SettingValue) => void; error?: string;
}) {
  const id = `setting-${spec.name}`;
  const common = cx("input", error && "border-danger/60");
  if (spec.kind === "bool") {
    return (
      <button type="button" role="switch" id={id} aria-checked={Boolean(value)} aria-label={spec.label}
              onClick={() => onChange(!value)}
              className={cx("relative h-6 w-11 rounded-full border transition-colors duration-200",
                            value ? "border-accent/60 bg-accent/40" : "border-line bg-white/[0.06]")}>
        <span className={cx("absolute top-0.5 w-[18px] rounded-full bg-ink transition-all duration-200",
                            value ? "left-[22px]" : "left-0.5")} style={{ height: 18 }} />
      </button>
    );
  }
  if (spec.kind === "choice") {
    return (
      <select id={id} className={common} value={String(value ?? "")} onChange={(e) => onChange(e.target.value)}>
        {spec.options.map((option) => {
          const unavailable = spec.unavailable_options?.[option];
          return (
            <option key={option} value={option} disabled={Boolean(unavailable)} title={unavailable || undefined}>
              {unavailable ? `${option} — unavailable: ${unavailable}` : option}
            </option>
          );
        })}
      </select>
    );
  }
  if (spec.kind === "int" || spec.kind === "float") {
    if (spec.nullable) {
      const enabled = value !== null && value !== undefined;
      return (
        <div className="flex items-center gap-2">
          <input type="checkbox" aria-label={`Set ${spec.label}`} checked={enabled}
                 className="h-4 w-4 accent-[rgb(var(--c-accent))]"
                 onChange={(e) => onChange(e.target.checked ? (spec.min ?? 0) + ((spec.max ?? 1) - (spec.min ?? 0)) * 0.4 : null)} />
          <input id={id} type="number" className={common} disabled={!enabled} min={spec.min ?? undefined}
                 max={spec.max ?? undefined} step={spec.step ?? "any"} value={enabled ? String(value) : ""}
                 placeholder="provider default"
                 onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))} />
        </div>
      );
    }
    return (
      <input id={id} type="number" inputMode={spec.kind === "int" ? "numeric" : "decimal"} className={common}
             min={spec.min ?? undefined} max={spec.max ?? undefined}
             step={spec.step ?? (spec.kind === "int" ? 1 : "any")} value={value === null ? "" : String(value)}
             onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))} />
    );
  }
  return (
    <input id={id} type="text" className={cx(common, "font-mono")} dir="ltr" spellCheck={false}
           value={String(value ?? "")} placeholder={spec.allow_empty ? "(empty = research model)" : undefined}
           onChange={(e) => onChange(e.target.value)} />
  );
}

export function RunSettingsForm({ contract, values, onChange, profile, appliesTo = "this run only" }: {
  contract: RunSettingsContract; values: SettingsValues; onChange: (values: SettingsValues) => void; profile: string;
  appliesTo?: string;
}) {
  const named = contract.named_profiles.some((p) => p.id === profile);
  const errors = useMemo(() => settingsErrors(contract, values), [contract, values]);
  const changed = Object.keys(overridesOf(contract, values));
  const set = (name: string, value: SettingValue) => onChange({ ...values, [name]: value });

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-card border border-line bg-white/[0.03] px-4 py-3 text-sm">
        <span className="text-ink-muted">
          Applies to <strong className="text-ink">{appliesTo}</strong>. The server&apos;s environment and other runs are
          never changed. {changed.length ? `${changed.length} setting${changed.length === 1 ? "" : "s"} changed.` : "All defaults."}
        </span>
        <Button size="sm" variant="ghost" disabled={!changed.length} onClick={() => onChange(defaultsOf(contract))}>
          Reset to server defaults
        </Button>
      </div>
      {named && (
        <p className="text-xs text-ink-muted">
          <Badge tone="violet" className="mr-2">Pinned</Badge>{contract.profile_note}
        </p>
      )}
      {contract.groups.map((group) => (
        <fieldset key={group} className="space-y-3">
          <legend className="kicker mb-2">{group}</legend>
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
            {contract.settings.filter((s) => s.group === group).map((spec) => {
              const value = valueOf(values, spec);
              const pinned = named && spec.pinned_by_named_profile;
              return (
                <div key={spec.name} className={cx("space-y-1.5 rounded-xl border p-3 transition-colors",
                                                   value !== spec.default ? "border-accent/35 bg-accent/[0.05]" : "border-transparent",
                                                   pinned && "opacity-60")}>
                  <div className="flex items-start justify-between gap-2">
                    <label htmlFor={`setting-${spec.name}`} className="text-xs font-medium text-ink">{spec.label}</label>
                    {pinned && <Badge tone="violet" className="shrink-0 text-[10px]" title={contract.profile_note}>Pinned</Badge>}
                  </div>
                  <SettingInput spec={spec} value={value} error={errors[spec.name]} onChange={(v) => set(spec.name, v)} />
                  {errors[spec.name] ? <p className="text-[11px] text-danger">{errors[spec.name]}</p>
                    : <p className="text-[11px] text-ink-faint">
                        default {spec.default === null ? "not set" : spec.default === "" ? "empty" : String(spec.default)}
                        {spec.min !== null && spec.max !== null && ` · ${spec.min}–${spec.max}`}
                      </p>}
                  {spec.help && <p className="text-[11px] leading-snug text-ink-faint">{spec.help}</p>}
                </div>
              );
            })}
          </div>
        </fieldset>
      ))}
      <div className="rounded-card border border-line bg-white/[0.02] p-4">
        <p className="kicker mb-2">Server-controlled</p>
        <ul className="grid gap-2 text-xs text-ink-muted sm:grid-cols-2">
          {contract.server_controlled.map((s) => (
            <li key={s.name}><span className="text-ink">{s.label}</span> — {s.reason}</li>
          ))}
        </ul>
      </div>
    </div>
  );
}
