import type { Locale } from "./config";

export type FacetKind = "brand" | "colour" | "size" | "category";
export type DegradedReason = "rerank" | "llm_parse" | "knn" | "bm25" | "semantic_cache";

export interface Messages {
  siteTitle: string;
  searchLabel: string;
  searchPlaceholder: string;
  searchButton: string;
  skipToResults: string;
  switchLanguage: string;
  /** Name of the *other* language, shown on the locale switch in that language. */
  otherLanguageName: string;
  emptyState: string;
  resultCount: string;
  noResults: string;
  refine: string;
  selected: string;
  facetNames: Record<FacetKind, string>;
  price: string;
  priceMin: string;
  priceMax: string;
  apply: string;
  appliedFilters: string;
  removeFilter: string;
  clearAll: string;
  fromQuery: string;
  interpretationTitle: string;
  interpretation: {
    original: string;
    normalised: string;
    detectedLang: string;
    script: string;
    rewritten: string;
    parser: string;
    filters: string;
    unparsed: string;
    none: string;
  };
  parserNames: Record<string, string>;
  scriptNames: Record<string, string>;
  languageNames: Record<string, string>;
  degradedTitle: string;
  degradedReasons: Record<DegradedReason, string>;
  degradedUnknown: string;
  pagination: string;
  previous: string;
  next: string;
  page: string;
  searchUnavailable: string;
  resultsExpired: string;
  startOver: string;
}

const en: Messages = {
  siteTitle: "Souq",
  searchLabel: "Search the catalogue",
  searchPlaceholder: "Try “black sony headphones under 200” or “سماعات سوني”",
  searchButton: "Search",
  skipToResults: "Skip to results",
  switchLanguage: "Switch language",
  otherLanguageName: "العربية",
  emptyState: "Search the catalogue in English or Arabic. Filters like brand, colour and price are picked up from your query.",
  resultCount: "About {count} results",
  noResults: "No products matched “{q}”. Try fewer words or remove a filter.",
  refine: "Refine",
  selected: "selected",
  facetNames: { brand: "Brand", colour: "Colour", size: "Size", category: "Category" },
  price: "Price",
  priceMin: "Min",
  priceMax: "Max",
  apply: "Apply",
  appliedFilters: "Applied filters",
  removeFilter: "Remove filter: {label}",
  clearAll: "Clear all",
  fromQuery: "from your query",
  interpretationTitle: "Query interpretation",
  interpretation: {
    original: "You searched",
    normalised: "Normalised",
    detectedLang: "Detected language",
    script: "Script",
    rewritten: "Rewritten as",
    parser: "Parsed by",
    filters: "Extracted filters",
    unparsed: "Unparsed terms",
    none: "None",
  },
  parserNames: { rule: "rules", llm: "language model", cache: "cache" },
  scriptNames: { ar: "Arabic", en: "Latin", mixed: "Mixed", arabizi: "Arabizi" },
  languageNames: { en: "English", ar: "Arabic", auto: "Auto" },
  degradedTitle: "Results may be less relevant than usual",
  degradedReasons: {
    rerank: "Re-ranking was skipped, so results are in retrieval order.",
    llm_parse: "The smart query parser was unavailable, so basic rules interpreted your query.",
    knn: "Semantic matching was unavailable; only keyword matches are shown.",
    bm25: "Keyword matching was unavailable; only semantic matches are shown.",
    semantic_cache: "The query cache was unavailable.",
  },
  degradedUnknown: "Part of the search pipeline was unavailable ({reason}).",
  pagination: "Pagination",
  previous: "Previous",
  next: "Next",
  page: "Page {page}",
  searchUnavailable: "Search is unavailable right now. Please try again in a moment.",
  resultsExpired: "These results have expired.",
  startOver: "Start again from the first page",
};

const ar: Messages = {
  siteTitle: "سوق",
  searchLabel: "ابحث في الكتالوج",
  searchPlaceholder: "جرّب «سماعات سوني سوداء أقل من 200» أو “sony headphones”",
  searchButton: "بحث",
  skipToResults: "انتقل إلى النتائج",
  switchLanguage: "تغيير اللغة",
  otherLanguageName: "English",
  emptyState: "ابحث في الكتالوج بالعربية أو الإنجليزية. يتم التعرف على عوامل التصفية مثل العلامة التجارية واللون والسعر من استعلامك.",
  resultCount: "حوالي {count} نتيجة",
  noResults: "لا توجد منتجات مطابقة لـ «{q}». جرّب كلمات أقل أو أزل أحد عوامل التصفية.",
  refine: "تصفية النتائج",
  selected: "محدد",
  facetNames: { brand: "العلامة التجارية", colour: "اللون", size: "المقاس", category: "الفئة" },
  price: "السعر",
  priceMin: "الحد الأدنى",
  priceMax: "الحد الأقصى",
  apply: "تطبيق",
  appliedFilters: "عوامل التصفية المطبقة",
  removeFilter: "إزالة عامل التصفية: {label}",
  clearAll: "مسح الكل",
  fromQuery: "من استعلامك",
  interpretationTitle: "تفسير الاستعلام",
  interpretation: {
    original: "بحثت عن",
    normalised: "بعد التطبيع",
    detectedLang: "اللغة المكتشفة",
    script: "نظام الكتابة",
    rewritten: "أعيدت صياغته إلى",
    parser: "المحلل",
    filters: "عوامل التصفية المستخرجة",
    unparsed: "مصطلحات غير محللة",
    none: "لا يوجد",
  },
  parserNames: { rule: "القواعد", llm: "نموذج لغوي", cache: "الذاكرة المؤقتة" },
  scriptNames: { ar: "عربي", en: "لاتيني", mixed: "مختلط", arabizi: "عربيزي" },
  languageNames: { en: "الإنجليزية", ar: "العربية", auto: "تلقائي" },
  degradedTitle: "قد تكون النتائج أقل دقة من المعتاد",
  degradedReasons: {
    rerank: "تم تخطي إعادة الترتيب، لذا تظهر النتائج بترتيب الاسترجاع.",
    llm_parse: "محلل الاستعلامات الذكي غير متاح، لذا فُسّر استعلامك بالقواعد الأساسية.",
    knn: "المطابقة الدلالية غير متاحة؛ تظهر نتائج الكلمات المفتاحية فقط.",
    bm25: "مطابقة الكلمات المفتاحية غير متاحة؛ تظهر النتائج الدلالية فقط.",
    semantic_cache: "ذاكرة الاستعلامات المؤقتة غير متاحة.",
  },
  degradedUnknown: "جزء من خدمة البحث غير متاح ({reason}).",
  pagination: "التنقل بين الصفحات",
  previous: "السابق",
  next: "التالي",
  page: "الصفحة {page}",
  searchUnavailable: "البحث غير متاح حاليًا. يرجى المحاولة مرة أخرى بعد قليل.",
  resultsExpired: "انتهت صلاحية هذه النتائج.",
  startOver: "ابدأ من الصفحة الأولى",
};

const catalog: Record<Locale, Messages> = { en, ar };

export function messagesFor(locale: Locale): Messages {
  return catalog[locale];
}

/** Replaces `{name}` placeholders; unknown placeholders are left visible so missing values are obvious. */
export function format(template: string, values: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (match, key: string) =>
    key in values ? String(values[key]) : match,
  );
}
