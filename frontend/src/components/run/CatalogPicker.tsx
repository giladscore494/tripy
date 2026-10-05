// The live MILO catalog browser of New research (PR #47, B1): cascading pickers (manufacturer -> model -> year ->
// trim) and a record-id search over GET /api/catalog*, read-only. The chosen vehicles (at most `max`) are listed
// before the run starts. Without DATABASE_URL the server answers in snapshot mode and the browser is disabled.

import { useMemo, useState } from "react";

import { api } from "../../api/endpoints";
import type { CatalogItem, CatalogQuery, CatalogStatus } from "../../api/types";
import { useResource } from "../../hooks/useResource";
import { Badge, Button, Dir, ErrorState, Field, Mono, Notice, Skeleton } from "../ui/primitives";

export function CatalogPicker({ status, selected, onChange, max = 50 }: {
  status: CatalogStatus | undefined; selected: CatalogItem[]; onChange: (items: CatalogItem[]) => void; max?: number;
}) {
  const enabled = Boolean(status?.browser_enabled);
  const [manufacturer, setManufacturer] = useState("");
  const [model, setModel] = useState("");
  const [year, setYear] = useState("");
  const [trim, setTrim] = useState("");
  const [q, setQ] = useState("");
  const [offset, setOffset] = useState(0);

  const makers = useResource(enabled ? "catalog-makers" : null, (signal) => api.catalogManufacturers({ signal }));
  const models = useResource(enabled && manufacturer ? `catalog-models:${manufacturer}` : null,
                             (signal) => api.catalogModels(manufacturer, { signal }));
  const years = useResource(enabled && manufacturer && model ? `catalog-years:${manufacturer}|${model}` : null,
                            (signal) => api.catalogYears(manufacturer, model, { signal }));
  const trims = useResource(enabled && manufacturer && model && year ? `catalog-trims:${manufacturer}|${model}|${year}` : null,
                            (signal) => api.catalogTrims(manufacturer, model, Number(year), { signal }));
  const query: CatalogQuery | null = useMemo(() => {
    const recordId = /^\d+$/.test(q.trim()) ? q.trim() : "";
    if (!(recordId || (manufacturer && (model || year)))) return null;
    return { manufacturer: manufacturer || undefined, model: model || undefined, year: year ? Number(year) : undefined,
             trim: trim || undefined, q: q.trim() || undefined, limit: 50, offset };
  }, [manufacturer, model, year, trim, q, offset]);
  const page = useResource(enabled && query ? `catalog:${JSON.stringify(query)}` : null,
                           (signal) => api.catalogSearch(query as CatalogQuery, { signal }));
  const chosen = new Set(selected.map((v) => v.record_id));

  if (!status) return <Skeleton className="h-24 rounded-card" />;
  if (!enabled) {
    return (
      <Notice tone="neutral" title={`Level 1.5 source: ${status.label}`}>
        The live catalog browser needs the server&apos;s DATABASE_URL. Without it, research runs on the 50 benchmark
        records (the other scopes).
      </Notice>
    );
  }
  const toggle = (item: CatalogItem) => {
    if (chosen.has(item.record_id)) onChange(selected.filter((v) => v.record_id !== item.record_id));
    else if (selected.length < max) onChange([...selected, item]);
  };
  const reset = (level: number) => {
    if (level <= 0) setModel("");
    if (level <= 1) setYear("");
    if (level <= 2) setTrim("");
    setOffset(0);
  };
  const items = page.data?.items ?? [];
  return (
    <div className="space-y-4">
      <p className="text-sm text-ink-muted">Source: <span className="text-ink">{status.label}</span></p>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Field label="Manufacturer" htmlFor="cat-manufacturer">
          <select id="cat-manufacturer" className="input" dir="auto" value={manufacturer}
                  onChange={(e) => { setManufacturer(e.target.value); reset(0); }}>
            <option value="">Choose…</option>
            {makers.data?.manufacturers.map((m) => <option key={m.manufacturer} value={m.manufacturer}>{m.manufacturer}</option>)}
          </select>
        </Field>
        <Field label="Model" htmlFor="cat-model">
          <select id="cat-model" className="input" dir="auto" value={model} disabled={!manufacturer}
                  onChange={(e) => { setModel(e.target.value); reset(1); }}>
            <option value="">{manufacturer ? "Any model" : "—"}</option>
            {models.data?.models.map((m) => <option key={m.model} value={m.model}>{m.model} ({m.variants})</option>)}
          </select>
        </Field>
        <Field label="Model year" htmlFor="cat-year">
          <select id="cat-year" className="input" value={year} disabled={!model}
                  onChange={(e) => { setYear(e.target.value); reset(2); }}>
            <option value="">{model ? "Any year" : "—"}</option>
            {years.data?.years.map((y) => <option key={y.year} value={y.year}>{y.year} ({y.variants})</option>)}
          </select>
        </Field>
        <Field label="Trim" htmlFor="cat-trim">
          <select id="cat-trim" className="input" dir="auto" value={trim} disabled={!year}
                  onChange={(e) => { setTrim(e.target.value); setOffset(0); }}>
            <option value="">{year ? "Any trim" : "—"}</option>
            {trims.data?.trims.filter((t) => t.trim).map((t) => <option key={t.trim as string} value={t.trim as string}>{t.trim} ({t.variants})</option>)}
          </select>
        </Field>
      </div>
      <Field label="Record id" htmlFor="cat-q"
             hint="An upstream record id; with a manufacturer and a model year also a degem_cd (the indexed searches).">
        <input id="cat-q" className="input" inputMode="numeric" value={q} placeholder="e.g. 38626"
               onChange={(e) => { setQ(e.target.value); setOffset(0); }} />
      </Field>
      {page.error ? <ErrorState error={page.error} onRetry={page.refresh} />
        : !query ? <p className="text-sm text-ink-faint">Choose a manufacturer and a model (or a model year), or enter a record id.</p>
        : page.loading ? <Skeleton className="h-40 rounded-card" />
        : (
          <div className="space-y-2">
            <p className="text-xs text-ink-faint">{page.data?.total ?? 0} variants · showing {offset + 1}–{offset + items.length}</p>
            <ul role="listbox" aria-label="Catalog vehicles" aria-multiselectable
                className="max-h-80 space-y-1 overflow-y-auto rounded-card border border-line bg-white/[0.02] p-1.5">
              {items.map((item) => (
                <li key={item.record_id} role="option" aria-selected={chosen.has(item.record_id)}>
                  <button type="button" onClick={() => toggle(item)}
                          className={`flex w-full items-center justify-between gap-3 rounded-xl px-3 py-2 text-left text-sm ${
                            chosen.has(item.record_id) ? "bg-accent/15 text-ink" : "text-ink-muted hover:bg-white/[0.05] hover:text-ink"}`}>
                    <Dir className="min-w-0 break-words">{item.label}</Dir>
                    <Mono className="shrink-0 text-[11px]">{[item.body, item.propulsion, item.power_hp && `${item.power_hp} hp`].filter(Boolean).join(" · ")}</Mono>
                  </button>
                </li>
              ))}
              {!items.length && <li className="px-3 py-6 text-center text-sm text-ink-muted">No catalog variant matches.</li>}
            </ul>
            <div className="flex gap-2">
              <Button size="sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Previous</Button>
              <Button size="sm" disabled={offset + items.length >= (page.data?.total ?? 0)}
                      onClick={() => setOffset(offset + 50)}>Next</Button>
            </div>
          </div>
        )}
      <div aria-label="Selected vehicles" className="space-y-1.5">
        <p className="text-xs font-medium text-ink-muted">
          Selected <Badge tone={selected.length >= max ? "warn" : "accent"}>{selected.length} / {max}</Badge>
        </p>
        {selected.length > 0 && (
          <ul className="space-y-1 text-sm">
            {selected.map((v) => (
              <li key={v.record_id} className="flex items-center justify-between gap-3">
                <Dir className="min-w-0 break-words text-ink">{v.label}</Dir>
                <Button size="sm" variant="ghost" onClick={() => toggle(v)}>Remove</Button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
