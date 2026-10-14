import Link from "next/link";
import type { Locale } from "@/i18n/config";
import { format, messagesFor } from "@/i18n/messages";
import { formatCount } from "@/lib/format";
import { hrefFor, nextPage, pageNumber, previousPage, type SearchState } from "@/lib/search-state";
import type { PageInfo } from "@/lib/types";

/**
 * Next follows `pageInfo.endCursor`; previous replays the token stored in the URL history (the API is
 * forward-only). No arrow glyphs: words read correctly in both directions without mirroring tricks.
 */
export function Pagination({ locale, state, pageInfo }: { locale: Locale; state: SearchState; pageInfo: PageInfo }) {
  const m = messagesFor(locale);
  const prev = previousPage(state);
  const endCursor = pageInfo.endCursor;
  const next = pageInfo.hasNextPage && endCursor ? nextPage(state, endCursor) : null;
  if (!prev && !next) return null;
  return (
    <nav className="pagination" aria-label={m.pagination}>
      {prev ? (
        <Link prefetch={false} rel="prev" href={hrefFor(locale, prev)} className="page-link">
          {m.previous}
        </Link>
      ) : (
        <span className="page-link" aria-disabled="true">
          {m.previous}
        </span>
      )}
      <span className="page-current" aria-current="page">
        {format(m.page, { page: formatCount(pageNumber(state), locale) })}
      </span>
      {next ? (
        <Link prefetch={false} rel="next" href={hrefFor(locale, next)} className="page-link">
          {m.next}
        </Link>
      ) : (
        <span className="page-link" aria-disabled="true">
          {m.next}
        </span>
      )}
    </nav>
  );
}
