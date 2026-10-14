import { type Locale, intlLocale } from "@/i18n/config";

export function formatPrice(amount: number, currency: string, locale: Locale): string {
  try {
    return new Intl.NumberFormat(intlLocale(locale), { style: "currency", currency }).format(amount);
  } catch {
    // Unknown ISO code from a supplier feed: still show the amount rather than failing the whole page.
    return `${new Intl.NumberFormat(intlLocale(locale)).format(amount)} ${currency}`;
  }
}

export interface PricePart {
  text: string;
  /** Currency symbols like "US$" mix Latin letters with a neutral "$"; isolating them keeps RTL from reordering. */
  isolate: boolean;
}

export function formatPriceParts(amount: number, currency: string, locale: Locale): PricePart[] {
  try {
    return new Intl.NumberFormat(intlLocale(locale), { style: "currency", currency })
      .formatToParts(amount)
      .map((p) => ({ text: p.value, isolate: p.type === "currency" }));
  } catch {
    return [
      { text: new Intl.NumberFormat(intlLocale(locale)).format(amount), isolate: false },
      { text: " ", isolate: false },
      { text: currency, isolate: true },
    ];
  }
}

export function formatCount(n: number, locale: Locale): string {
  return new Intl.NumberFormat(intlLocale(locale)).format(n);
}

/** "Electronics > Audio > Headphones" → ["Electronics", "Audio", "Headphones"]. */
export function categoryLevels(path: string): string[] {
  return path
    .split(">")
    .map((s) => s.trim())
    .filter(Boolean);
}
