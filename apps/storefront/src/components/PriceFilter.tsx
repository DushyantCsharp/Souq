import type { Locale } from "@/i18n/config";
import { messagesFor } from "@/i18n/messages";
import { PARAM, type SearchState } from "@/lib/search-state";

/** GET form that re-submits the current query and filters with the new price bounds (page tokens dropped). */
export function PriceFilter({ locale, state }: { locale: Locale; state: SearchState }) {
  const m = messagesFor(locale);
  const { filters } = state;
  return (
    <form action={`/${locale}`} method="get" className="facet price-filter" aria-labelledby="facet-price">
      <h3 id="facet-price">{m.price}</h3>
      <input type="hidden" name={PARAM.q} value={state.q} />
      {filters.brands.map((v) => (
        <input key={`b-${v}`} type="hidden" name={PARAM.brand} value={v} />
      ))}
      {filters.colours.map((v) => (
        <input key={`c-${v}`} type="hidden" name={PARAM.colour} value={v} />
      ))}
      {filters.sizes.map((v) => (
        <input key={`s-${v}`} type="hidden" name={PARAM.size} value={v} />
      ))}
      {filters.category && <input type="hidden" name={PARAM.category} value={filters.category} />}
      {filters.currency && <input type="hidden" name={PARAM.currency} value={filters.currency} />}
      <div className="price-inputs">
        <label>
          <span>{m.priceMin}</span>
          <input
            type="number"
            name={PARAM.priceMin}
            min={0}
            step="any"
            inputMode="decimal"
            defaultValue={filters.priceMin ?? ""}
          />
        </label>
        <label>
          <span>{m.priceMax}</span>
          <input
            type="number"
            name={PARAM.priceMax}
            min={0}
            step="any"
            inputMode="decimal"
            defaultValue={filters.priceMax ?? ""}
          />
        </label>
      </div>
      <button type="submit">{m.apply}</button>
    </form>
  );
}
