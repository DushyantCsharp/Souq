import Link from "next/link";
import { type Locale, otherLocale } from "@/i18n/config";
import { messagesFor } from "@/i18n/messages";
import { firstPage, hrefFor, type SearchState } from "@/lib/search-state";

/** Keeps the query and filters; drops the page tokens because they are bound to the search language. */
export function LocaleSwitch({ locale, state }: { locale: Locale; state: SearchState }) {
  const target = otherLocale(locale);
  const m = messagesFor(locale);
  return (
    <Link
      prefetch={false}
      href={hrefFor(target, firstPage(state))}
      hrefLang={target}
      lang={target}
      className="locale-switch"
      aria-label={`${m.switchLanguage}: ${m.otherLanguageName}`}
    >
      {m.otherLanguageName}
    </Link>
  );
}
