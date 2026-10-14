import type { Locale } from "@/i18n/config";
import { type Messages, messagesFor } from "@/i18n/messages";
import type { Interpretation } from "@/lib/types";

type ParsedFilters = Interpretation["filters"];

function filterEntries(m: Messages, filters: ParsedFilters | null | undefined): Array<[string, string]> {
  if (!filters) return [];
  const out: Array<[string, string]> = [];
  const join = (values: readonly string[] | null | undefined) => (values ?? []).join(", ");
  if (filters.brands?.length) out.push([m.facetNames.brand, join(filters.brands)]);
  if (filters.colours?.length) out.push([m.facetNames.colour, join(filters.colours)]);
  if (filters.sizes?.length) out.push([m.facetNames.size, join(filters.sizes)]);
  if (filters.category) out.push([m.facetNames.category, filters.category]);
  if (filters.priceMin != null) out.push([m.priceMin, String(filters.priceMin)]);
  if (filters.priceMax != null) out.push([m.priceMax, String(filters.priceMax)]);
  if (filters.currency) out.push([m.price, filters.currency]);
  return out;
}

/**
 * Shows what the search service understood: normalised text, detected language/script, which parser ran
 * (rule | llm | cache) and the structured filters it extracted. Values come from `parsed_query` verbatim.
 */
export function QueryInterpretationPanel({
  locale,
  interpretation,
}: {
  locale: Locale;
  interpretation: Interpretation;
}) {
  const m = messagesFor(locale);
  const i = interpretation;
  const t = m.interpretation;
  const filters = filterEntries(m, i.filters);
  const unparsed = i.unparsedTokens ?? [];
  return (
    <details className="interpretation" open data-parser={i.parser}>
      <summary>{m.interpretationTitle}</summary>
      <dl>
        <dt>{t.original}</dt>
        <dd>
          <bdi>{i.original}</bdi>
        </dd>
        <dt>{t.normalised}</dt>
        <dd>
          <bdi>{i.normalised}</bdi>
        </dd>
        {i.rewritten && (
          <>
            <dt>{t.rewritten}</dt>
            <dd>
              <bdi>{i.rewritten}</bdi>
            </dd>
          </>
        )}
        <dt>{t.detectedLang}</dt>
        <dd>{m.languageNames[i.detectedLang.toLowerCase()] ?? i.detectedLang}</dd>
        <dt>{t.script}</dt>
        <dd>{m.scriptNames[i.script.toLowerCase()] ?? i.script}</dd>
        <dt>{t.parser}</dt>
        <dd>{m.parserNames[i.parser.toLowerCase()] ?? i.parser}</dd>
        <dt>{t.filters}</dt>
        <dd>
          {filters.length === 0 ? (
            t.none
          ) : (
            <ul className="inline-list">
              {filters.map(([name, value]) => (
                <li key={name}>
                  {name}: <bdi>{value}</bdi>
                </li>
              ))}
            </ul>
          )}
        </dd>
        <dt>{t.unparsed}</dt>
        <dd>
          {unparsed.length === 0 ? (
            t.none
          ) : (
            <ul className="inline-list">
              {unparsed.map((token, idx) => (
                <li key={`${idx}-${token}`}>
                  <bdi>{token}</bdi>
                </li>
              ))}
            </ul>
          )}
        </dd>
      </dl>
    </details>
  );
}
