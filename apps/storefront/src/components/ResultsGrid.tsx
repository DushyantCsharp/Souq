import type { Locale } from "@/i18n/config";
import type { ProductEdge } from "@/lib/types";
import { ProductCard } from "./ProductCard";

export function ResultsGrid({ locale, edges }: { locale: Locale; edges: readonly ProductEdge[] }) {
  return (
    <ol className="results-grid">
      {edges.map((edge) => (
        <li key={edge.node.sku}>
          <ProductCard locale={locale} item={edge.node} />
        </li>
      ))}
    </ol>
  );
}
