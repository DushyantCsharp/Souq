import Link from "next/link";
import type { Locale } from "@/i18n/config";
import { format, messagesFor } from "@/i18n/messages";
import {
  clearFilters,
  hasFilters,
  hrefFor,
  removePrice,
  type SearchState,
  toggleFilter,
} from "@/lib/search-state";
import type { AppliedFilters as AppliedFiltersData } from "@/lib/types";

interface Chip {
  key: string;
  label: string;
  /** Present for filters the shopper chose (in the URL); absent for ones the parser took from the query. */
  removeHref?: string;
}

function chipsFor(locale: Locale, state: SearchState, applied: AppliedFiltersData | null | undefined): Chip[] {
  const m = messagesFor(locale);
  const f = state.filters;
  const chips: Chip[] = [];
  const list = (kind: "brand" | "colour" | "size", chosen: string[], fromApi: readonly string[]) => {
    for (const v of chosen) {
      chips.push({ key: `${kind}:${v}`, label: v, removeHref: hrefFor(locale, toggleFilter(state, kind, v)) });
    }
    for (const v of fromApi) if (!chosen.includes(v)) chips.push({ key: `${kind}:${v}`, label: v });
  };
  list("brand", f.brands, applied?.brands ?? []);
  list("colour", f.colours, applied?.colours ?? []);
  list("size", f.sizes, applied?.sizes ?? []);

  if (f.category) {
    chips.push({
      key: `category:${f.category}`,
      label: f.category,
      removeHref: hrefFor(locale, toggleFilter(state, "category", f.category)),
    });
  } else if (applied?.category) {
    chips.push({ key: `category:${applied.category}`, label: applied.category });
  }

  const price = (bound: "priceMin" | "priceMax", chosen?: number, fromApi?: number | null) => {
    const name = bound === "priceMin" ? m.priceMin : m.priceMax;
    if (chosen !== undefined) {
      chips.push({ key: bound, label: `${name} ${chosen}`, removeHref: hrefFor(locale, removePrice(state, bound)) });
    } else if (fromApi != null) {
      chips.push({ key: bound, label: `${name} ${fromApi}` });
    }
  };
  price("priceMin", f.priceMin, applied?.priceMin);
  price("priceMax", f.priceMax, applied?.priceMax);
  return chips;
}

export function AppliedFilters({
  locale,
  state,
  applied,
}: {
  locale: Locale;
  state: SearchState;
  applied?: AppliedFiltersData | null;
}) {
  const m = messagesFor(locale);
  const chips = chipsFor(locale, state, applied);
  if (chips.length === 0) return null;
  return (
    <section className="applied-filters" aria-labelledby="applied-heading">
      <h2 id="applied-heading" className="visually-hidden">
        {m.appliedFilters}
      </h2>
      <ul>
        {chips.map((chip) => (
          <li key={chip.key} className="chip" data-source={chip.removeHref ? "user" : "query"}>
            <bdi>{chip.label}</bdi>
            {chip.removeHref ? (
              <Link
                prefetch={false}
                rel="nofollow"
                href={chip.removeHref}
                className="chip-remove"
                aria-label={format(m.removeFilter, { label: chip.label })}
              >
                ×
              </Link>
            ) : (
              <span className="chip-note">{m.fromQuery}</span>
            )}
          </li>
        ))}
      </ul>
      {hasFilters(state.filters) && (
        <Link prefetch={false} href={hrefFor(locale, clearFilters(state))} className="clear-all">
          {m.clearAll}
        </Link>
      )}
    </section>
  );
}
