import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { AppliedFilters } from "@/components/AppliedFilters";
import { DegradedNotice } from "@/components/DegradedNotice";
import { FacetsSidebar } from "@/components/FacetsSidebar";
import { LocaleSwitch } from "@/components/LocaleSwitch";
import { Pagination } from "@/components/Pagination";
import { QueryInterpretationPanel } from "@/components/QueryInterpretationPanel";
import { ResultsGrid } from "@/components/ResultsGrid";
import { SearchBox } from "@/components/SearchBox";
import { isLocale, type Locale } from "@/i18n/config";
import { format, messagesFor } from "@/i18n/messages";
import { formatCount } from "@/lib/format";
import { GatewayError, gatewayQuery } from "@/lib/gateway";
import { StorefrontSearchDocument } from "@/lib/operations";
import { firstPage, hrefFor, parseSearchState, type RawSearchParams, type SearchState } from "@/lib/search-state";
import type { SearchResult } from "@/lib/types";
import { toSearchVariables } from "@/lib/variables";

interface PageProps {
  params: Promise<{ locale: string }>;
  searchParams: Promise<RawSearchParams>;
}

export async function generateMetadata({ searchParams }: PageProps): Promise<Metadata> {
  const { q } = parseSearchState(await searchParams);
  // Result pages are per-query and per-cursor; keep them out of search-engine indexes.
  return q ? { title: q, robots: { index: false } } : {};
}

type Outcome =
  | { kind: "idle" }
  | { kind: "ok"; result: SearchResult }
  | { kind: "error"; error: GatewayError };

async function runSearch(state: SearchState, locale: Locale): Promise<Outcome> {
  if (!state.q) return { kind: "idle" };
  try {
    const data = await gatewayQuery(StorefrontSearchDocument, toSearchVariables(state, locale));
    return { kind: "ok", result: data.search };
  } catch (error) {
    if (error instanceof GatewayError) {
      console.error(`[storefront] search failed (${error.kind}): ${error.message}`);
      return { kind: "error", error };
    }
    throw error;
  }
}

export default async function SearchPage({ params, searchParams }: PageProps) {
  const { locale: rawLocale } = await params;
  if (!isLocale(rawLocale)) notFound();
  const locale = rawLocale;
  const m = messagesFor(locale);
  const state = parseSearchState(await searchParams);
  const outcome = await runSearch(state, locale);

  return (
    <div className="page">
      <a href="#results" className="skip-link">
        {m.skipToResults}
      </a>
      <header className="site-header">
        <Link prefetch={false} href={`/${locale}`} className="brand">
          {m.siteTitle}
        </Link>
        <SearchBox locale={locale} q={state.q} />
        <LocaleSwitch locale={locale} state={state} />
      </header>

      {outcome.kind === "idle" && (
        <main id="results" className="empty-state">
          <p>{m.emptyState}</p>
        </main>
      )}

      {outcome.kind === "error" && (
        <main id="results" className="notice notice-error" role="alert">
          {outcome.error.kind === "invalid_page_token" ? (
            <p>
              {m.resultsExpired}{" "}
              <Link prefetch={false} href={hrefFor(locale, firstPage(state))}>
                {m.startOver}
              </Link>
            </p>
          ) : (
            <p>{m.searchUnavailable}</p>
          )}
        </main>
      )}

      {outcome.kind === "ok" && (
        <div className="layout">
          <FacetsSidebar locale={locale} state={state} facets={outcome.result.facets} />
          <main id="results" className="results" aria-live="polite">
            <p className="result-count">
              {format(m.resultCount, { count: formatCount(outcome.result.totalEstimate, locale) })}
            </p>
            <AppliedFilters locale={locale} state={state} applied={outcome.result.appliedFilters} />
            <DegradedNotice locale={locale} degraded={outcome.result.degraded} />
            <QueryInterpretationPanel locale={locale} interpretation={outcome.result.interpretation} />
            {outcome.result.edges.length === 0 ? (
              <p className="no-results">{format(m.noResults, { q: state.q })}</p>
            ) : (
              <ResultsGrid locale={locale} edges={outcome.result.edges} />
            )}
            <Pagination locale={locale} state={state} pageInfo={outcome.result.pageInfo} />
          </main>
        </div>
      )}
    </div>
  );
}
