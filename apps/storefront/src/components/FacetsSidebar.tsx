import Link from "next/link";
import type { Locale } from "@/i18n/config";
import { messagesFor } from "@/i18n/messages";
import { formatCount } from "@/lib/format";
import { hrefFor, isFacetKind, isFilterSelected, type SearchState, toggleFilter } from "@/lib/search-state";
import type { Facet } from "@/lib/types";
import { PriceFilter } from "./PriceFilter";

/**
 * Each bucket is a link to the same search with that value toggled. Facets the UI cannot filter on (names
 * other than brand/colour/size/category) are skipped rather than shown as dead options.
 */
export function FacetsSidebar({
  locale,
  state,
  facets,
}: {
  locale: Locale;
  state: SearchState;
  facets: readonly Facet[];
}) {
  const m = messagesFor(locale);
  return (
    <aside className="facets" aria-labelledby="facets-heading">
      <h2 id="facets-heading">{m.refine}</h2>
      {facets.map((facet) => {
        const kind = facet.name;
        if (!isFacetKind(kind) || facet.buckets.length === 0) return null;
        return (
          <section key={kind} className="facet" aria-labelledby={`facet-${kind}`}>
            <h3 id={`facet-${kind}`}>{m.facetNames[kind]}</h3>
            <ul>
              {facet.buckets.map((bucket) => {
                const selected = isFilterSelected(state, kind, bucket.value);
                return (
                  <li key={bucket.value}>
                    <Link
                      prefetch={false}
                      rel="nofollow"
                      href={hrefFor(locale, toggleFilter(state, kind, bucket.value))}
                      className="facet-option"
                      data-selected={selected ? "true" : "false"}
                    >
                      <span className="facet-check" aria-hidden="true" />
                      <bdi className="facet-value">{bucket.value}</bdi>
                      <span className="facet-count">{formatCount(bucket.count, locale)}</span>
                      {selected && <span className="visually-hidden"> ({m.selected})</span>}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </section>
        );
      })}
      <PriceFilter locale={locale} state={state} />
    </aside>
  );
}
