import type { Locale } from "@/i18n/config";
import { type DegradedReason, format, messagesFor } from "@/i18n/messages";

function isKnownReason(reason: string, known: Record<DegradedReason, string>): reason is DegradedReason {
  return reason in known;
}

/** Rendered only when the search service degraded a stage (CLAUDE.md rule 5: degrade, don't fail). */
export function DegradedNotice({ locale, degraded }: { locale: Locale; degraded: readonly string[] }) {
  if (degraded.length === 0) return null;
  const m = messagesFor(locale);
  return (
    <div role="status" className="notice notice-degraded" data-testid="degraded-notice">
      <p className="notice-title">{m.degradedTitle}</p>
      <ul>
        {/* GraphQL returns the Degradation enum (RERANK, LLM_PARSE, ...); the REST/search API uses lowercase. */}
        {[...new Set(degraded.map((r) => r.toLowerCase()))].map((reason) => (
          <li key={reason} data-reason={reason}>
            {isKnownReason(reason, m.degradedReasons)
              ? m.degradedReasons[reason]
              : format(m.degradedUnknown, { reason })}
          </li>
        ))}
      </ul>
    </div>
  );
}
