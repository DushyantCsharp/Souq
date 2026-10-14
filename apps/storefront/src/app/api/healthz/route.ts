// Liveness for the compose healthcheck. Deliberately does not call the gateway: a gateway outage should show
// the "search unavailable" page, not get the storefront container restarted.
export const dynamic = "force-dynamic";

export function GET(): Response {
  return Response.json({ status: "ok" });
}
