import type { Locale } from "@/i18n/config";
import { categoryLevels, formatPriceParts } from "@/lib/format";
import type { ProductItem } from "@/lib/types";

export function ProductCard({ locale, item }: { locale: Locale; item: ProductItem }) {
  const levels = categoryLevels(item.categoryPath);
  return (
    <article className="product-card" data-sku={item.sku} data-rank-source={item.rankSource ?? undefined}>
      {/* Titles fall back to the other language when a translation is missing, so let the text pick its direction. */}
      <h3 className="product-title" dir="auto">
        {item.title}
      </h3>
      <p className="product-brand">
        <bdi>{item.brand}</bdi>
      </p>
      <p className="product-price">
        {formatPriceParts(item.price.amount, item.price.currency, locale).map((part, i) =>
          part.isolate ? <bdi key={i}>{part.text}</bdi> : part.text,
        )}
      </p>
      {levels.length > 0 && (
        <p className="product-category">
          {levels.map((level, i) => (
            <span key={`${i}-${level}`}>
              <bdi>{level}</bdi>
            </span>
          ))}
        </p>
      )}
      {(item.colour || item.size) && (
        <p className="product-attrs">
          {item.colour && <span className="tag">{item.colour}</span>}
          {item.size && <span className="tag">{item.size}</span>}
        </p>
      )}
      {item.description && (
        <p className="product-description" dir="auto">
          {item.description}
        </p>
      )}
    </article>
  );
}
