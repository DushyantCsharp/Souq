"use client";

import { useParams } from "next/navigation";

// loading.tsx receives no props, so the locale comes from the route params on the client.
export default function Loading() {
  const params = useParams<{ locale: string }>();
  const label = params.locale === "ar" ? "جارٍ البحث…" : "Searching…";
  return (
    <div className="loading" role="status" aria-live="polite">
      {label}
    </div>
  );
}
