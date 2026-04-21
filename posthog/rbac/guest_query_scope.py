"""Server-side query rescoping for guest users on the /query/ endpoint.

Guests authenticate as regular session users and reach /query/ via the normal
request path; the `GuestDeflectionMiddleware` gates access (header must name a
granted resource), but the middleware does not inspect the query body.

Without this module, a guest with any valid scene-resource header could POST a
body like `{"query": {"kind": "EventsQuery", "select": ["*"]}}` and read all
team events. This module rescopes the query body before it reaches the query
runner:

1. Resolve the scene-resource header to a granted insight (for dashboard
   grants, the header identifies the tile's insight).
2. Load the insight's saved query from the DB (`Insight.query`).
3. Start from the saved query, overlay only whitelisted fields from the client
   body. The whitelist is generated from `@guestOverridable` JSDoc annotations
   in `frontend/src/queries/schema/schema-general.ts` (see
   `bin/generate-guest-overridable.py`).

The result is that the executed query's structural shape (kind, series,
source, HogQL text) comes from the saved insight. The client can only change
fields that correspond to UI-level viewer controls (date range, properties,
breakdown, filter test accounts, etc.).
"""

from __future__ import annotations

from typing import Any

from rest_framework.exceptions import NotFound
from rest_framework.request import Request

from posthog.models import GuestResourceGrant, OrganizationMembership
from posthog.models.insight import Insight
from posthog.rbac._generated_guest_overridable import GUEST_OVERRIDABLE_FIELDS

SCENE_RESOURCE_HEADER = "X-PostHog-Scene-Resource"


def user_is_guest(user) -> bool:
    if not user or not user.is_authenticated:
        return False
    return OrganizationMembership.objects.filter(user=user, is_guest=True).exists()


def rescope_guest_query(request: Request) -> None:
    """Mutate `request.data['query']` in place so only whitelisted fields from the
    client body are honored; everything else comes from the guest's granted
    resource. Raises NotFound if no grant matches the scene-resource header, if
    the saved query is missing, or if the client-submitted kind doesn't match
    the saved kind.
    """
    scene = request.headers.get(SCENE_RESOURCE_HEADER) or ""
    resource_type, _, resource_id = scene.partition(":")
    resource_type = resource_type.strip()
    resource_id = resource_id.strip()
    if resource_type not in ("dashboard", "insight") or not resource_id:
        raise NotFound()

    saved_insight = _load_insight_for_grant(request.user, resource_type, resource_id, request)
    if saved_insight is None or not isinstance(saved_insight.query, dict):
        raise NotFound()

    saved_query = _unwrap_insight_query(saved_insight.query)
    saved_kind = saved_query.get("kind")
    if not saved_kind:
        raise NotFound()

    client_query = ((request.data or {}).get("query") or {}) if isinstance(request.data, dict) else {}
    if not isinstance(client_query, dict):
        client_query = {}

    # Kind must match exactly. The scene-resource header binds the query to a
    # specific saved insight; a TrendsQuery grant cannot be used to run an
    # EventsQuery/ActorsQuery/HogQLQuery.
    if client_query.get("kind") and client_query["kind"] != saved_kind:
        raise NotFound()

    overridable = GUEST_OVERRIDABLE_FIELDS.get(saved_kind, frozenset())

    # Start from the saved query, overlay whitelisted fields from the client.
    # Any field not in the whitelist is discarded (including `series`, `source`,
    # `query` HogQL text, `events`, `actions`, etc.) — the structural shape of
    # the saved insight is preserved.
    rescoped = dict(saved_query)
    for field in overridable:
        if field in client_query:
            rescoped[field] = client_query[field]

    if not isinstance(request.data, dict):
        # Shouldn't happen for JSON-parsed requests, but defend anyway.
        raise NotFound()
    request.data["query"] = rescoped


def _load_insight_for_grant(user, resource_type: str, resource_id: str, request: Request) -> Insight | None:
    """Return the Insight whose saved query should be used for this request.

    For `insight` grants: look up the insight by short_id, verifying the grant.
    For `dashboard` grants: the header names a dashboard, but a query runs for
    a specific tile. The FE sends the tile insight's short_id alongside in the
    `client_query_id`-adjacent payload; we verify that short_id belongs to a
    tile of the granted dashboard before using it.
    """
    grants = GuestResourceGrant.objects.filter(
        organization_membership__user=user,
        organization_membership__is_guest=True,
        is_pending=False,
        resource=resource_type,
        resource_id=resource_id,
    )
    if not grants.exists():
        return None

    if resource_type == "insight":
        return Insight.objects.filter(short_id=resource_id, deleted=False).first()

    # dashboard grant — tile insight is named in the body via a sibling header
    # or in an FE-supplied field. Prefer an explicit tile short_id from the
    # request (header or body) over blindly picking a tile.
    tile_short_id = _tile_short_id_from_request(request)
    if not tile_short_id or not resource_id.isdigit():
        return None
    dashboard_id = int(resource_id)
    return (
        Insight.objects.filter(
            short_id=tile_short_id,
            deleted=False,
            dashboard_tiles__dashboard_id=dashboard_id,
        )
        .distinct()
        .first()
    )


def _tile_short_id_from_request(request: Request) -> str | None:
    """The FE stamps the tile insight's short_id in a sibling header alongside
    the dashboard scene resource. Keeps the dashboard-grant case unambiguous
    without requiring the client to restate which tile it is querying."""
    tile_header = request.headers.get("X-PostHog-Scene-Tile-Insight-Short-Id")
    if tile_header and tile_header.strip():
        return tile_header.strip()
    return None


def _unwrap_insight_query(query: dict[str, Any]) -> dict[str, Any]:
    """Saved insights store `InsightVizNode { source: TrendsQuery{...} }` wrappers.
    The /query/ endpoint expects the inner query node. Unwrap if present."""
    source = query.get("source") if query.get("kind") == "InsightVizNode" else None
    return source if isinstance(source, dict) else query
