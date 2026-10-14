import type { Locale } from "@/i18n/config";
import { messagesFor } from "@/i18n/messages";
import { PARAM } from "@/lib/search-state";

/**
 * Plain GET form: a new query starts a fresh search, so filters and page tokens are intentionally not
 * carried over (the search service re-derives filters from the new query text).
 */
export function SearchBox({ locale, q }: { locale: Locale; q: string }) {
  const m = messagesFor(locale);
  return (
    <form role="search" action={`/${locale}`} method="get" className="search-box">
      <label htmlFor="q" className="visually-hidden">
        {m.searchLabel}
      </label>
      {/* dir="auto": an Arabic query typed in the English UI (or vice versa) still renders in its own direction. */}
      <input
        id="q"
        name={PARAM.q}
        type="search"
        dir="auto"
        defaultValue={q}
        placeholder={m.searchPlaceholder}
        autoComplete="off"
        enterKeyHint="search"
        maxLength={256}
      />
      <button type="submit">{m.searchButton}</button>
    </form>
  );
}
