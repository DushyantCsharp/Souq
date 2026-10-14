// Next only discovers instrumentation in src/ when the app lives in src/app; the implementation is
// apps/storefront/instrumentation.ts (owned by observability).
export { register } from "../instrumentation";
