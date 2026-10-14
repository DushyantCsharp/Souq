"use client";

import { useParams } from "next/navigation";

// Last-resort boundary; expected gateway failures are handled in the page and never reach here.
export default function ErrorBoundary({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  const params = useParams<{ locale: string }>();
  const ar = params.locale === "ar";
  return (
    <div className="notice notice-error" role="alert">
      <p>{ar ? "حدث خطأ غير متوقع." : "Something went wrong."}</p>
      <button type="button" onClick={reset}>
        {ar ? "أعد المحاولة" : "Try again"}
      </button>
    </div>
  );
}
