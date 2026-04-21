"""Single-gate guest deflection middleware.

All guest enforcement runs through this middleware. The request either matches a rule in
`GUEST_RULES` that explicitly allows it, or the request is deflected — `404` for API paths
(so guest clients can't enumerate the surface area) or `redirect("/guest")` for non-API
SPA routes (so the FE scene allowlist/landing page can take over).

The middleware is authoritative. Viewsets and the `AccessControl` layer are NOT expected to
re-check guest status; once a request reaches its view, the guest is indistinguishable from
a regular viewer-level member on that resource (grant creation mirrors an AC row, see
`posthog.rbac.guest_grants`).

Adding support for a new resource type is a single entry in `GUEST_RULES`.
"""

import re
from typing import cast

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect

from posthog.models import GuestResourceGrant
from posthog.models.insight import Insight
from posthog.models.user import User

from products.dashboards.backend.models.dashboard_tile import DashboardTile


class GuestRule:
    """Base class for a guest deflection rule.

    Rules are tried in declaration order. The first rule whose `matches` returns a truthy
    match is the deciding rule: if its `allows` returns True the request is forwarded,
    otherwise the request is deflected. A request that matches no rule is deflected.
    """

    def __init__(self, pattern: str):
        self._pattern = re.compile(pattern)

    def matches(self, request: HttpRequest) -> re.Match | None:
        return self._pattern.match(request.path)

    def allows(self, request: HttpRequest, user: User, match: re.Match) -> bool:
        raise NotImplementedError


class AlwaysAllowed(GuestRule):
    """Identity, auth, static assets, the guest landing page, etc. — unconditionally allowed."""

    def allows(self, request: HttpRequest, user: User, match: re.Match) -> bool:
        return True


class TeamScopedMetadataRead(GuestRule):
    """GET-only team-scoped endpoints (themes, variables, tags, annotations, cohorts, quick filters).

    Insights and dashboards transitively depend on these to render. Allowed when the guest
    has any active grant in the team — otherwise they have no business reading team metadata.
    """

    def matches(self, request: HttpRequest) -> re.Match | None:
        if request.method != "GET":
            return None
        return super().matches(request)

    def allows(self, request: HttpRequest, user: User, match: re.Match) -> bool:
        team_id = int(match.group("team_id"))
        return GuestResourceGrant.objects.filter(
            organization_membership__user=user,
            organization_membership__is_guest=True,
            team_id=team_id,
        ).exists()


def _guest_grants_qs(user: User, team_id: int):
    return GuestResourceGrant.objects.filter(
        organization_membership__user=user,
        organization_membership__is_guest=True,
        team_id=team_id,
    )


def _insight_inherited_via_dashboard(user: User, team_id: int, resource_id: str) -> bool:
    """An insight is allowed if it is a tile of a granted dashboard.

    The URL-side insight id may be either a numeric PK or a `short_id` — mirror the viewset
    resolution order: numeric first, then short_id lookup.
    """
    insight_pk: int | None = None
    if resource_id.isdigit():
        insight_pk = int(resource_id)
    else:
        insight_pk = Insight.objects.filter(team_id=team_id, short_id=resource_id).values_list("id", flat=True).first()
    if insight_pk is None:
        return False
    parent_dashboard_ids = list(
        DashboardTile.objects.filter(insight_id=insight_pk).values_list("dashboard_id", flat=True)
    )
    if not parent_dashboard_ids:
        return False
    return (
        _guest_grants_qs(user, team_id)
        .filter(
            resource="dashboard",
            resource_id__in=[str(d) for d in parent_dashboard_ids],
        )
        .exists()
    )


class GrantBoundResource(GuestRule):
    """`/api/.../(dashboards|insights|notebooks)/<id>` — allowed iff the guest has a grant on the id.

    For insights, also allowed when the insight is a tile of a granted dashboard.
    """

    def __init__(self, resource: str, pattern: str):
        super().__init__(pattern)
        self._resource = resource

    def allows(self, request: HttpRequest, user: User, match: re.Match) -> bool:
        team_id = int(match.group("team_id"))
        resource_id = match.group("resource_id")
        qs = _guest_grants_qs(user, team_id)
        if qs.filter(resource=self._resource, resource_id=resource_id).exists():
            return True
        if self._resource == "insight":
            return _insight_inherited_via_dashboard(user, team_id, resource_id)
        return False


class GrantBoundListFilter(GuestRule):
    """GET `/api/.../<resource>/?short_id=<id>` — the FE scene loaders use this to resolve by short_id.

    Allowed when `short_id` names a granted resource; for insights also allowed via
    tile-of-granted-dashboard inheritance.
    """

    def __init__(self, resource: str, pattern: str, filter_key: str = "short_id"):
        super().__init__(pattern)
        self._resource = resource
        self._filter_key = filter_key

    def matches(self, request: HttpRequest) -> re.Match | None:
        if request.method != "GET":
            return None
        return super().matches(request)

    def allows(self, request: HttpRequest, user: User, match: re.Match) -> bool:
        filter_value = request.GET.get(self._filter_key)
        if not filter_value:
            return False
        team_id = int(match.group("team_id"))
        qs = _guest_grants_qs(user, team_id)
        if qs.filter(resource=self._resource, resource_id=filter_value).exists():
            return True
        if self._resource == "insight":
            return _insight_inherited_via_dashboard(user, team_id, filter_value)
        return False


class SceneBoundQuery(GuestRule):
    """`POST|GET /api/.../query[/<kind>]/` — allowed iff the `X-PostHog-Scene-Resource` header
    identifies a granted resource. Header format: `resource:resource_id`.

    This is the only binding source for queries; body keys such as `insight_id`/`dashboard_id`
    are not read here (design: one scene-context source for the whole SPA).
    """

    _SCENE_HEADER = "X-PostHog-Scene-Resource"
    _VALID_RESOURCES = ("dashboard", "insight", "notebook")

    def matches(self, request: HttpRequest) -> re.Match | None:
        if request.method not in {"POST", "GET"}:
            return None
        return super().matches(request)

    def allows(self, request: HttpRequest, user: User, match: re.Match) -> bool:
        header = request.headers.get(self._SCENE_HEADER)
        if not header or ":" not in header:
            return False
        resource, _, resource_id = header.partition(":")
        resource = resource.strip()
        resource_id = resource_id.strip()
        if not resource_id or resource not in self._VALID_RESOURCES:
            return False
        team_id = int(match.group("team_id"))
        qs = _guest_grants_qs(user, team_id)
        if qs.filter(resource=resource, resource_id=resource_id).exists():
            return True
        if resource == "insight":
            return _insight_inherited_via_dashboard(user, team_id, resource_id)
        return False


_METADATA_ENDPOINTS = (
    "data_color_themes",
    "insight_variables",
    "quick_filters",
    "annotations",
    "cohorts",
    "tags",
)


def _metadata_pattern(endpoint: str) -> str:
    return rf"^/api/(?:environments|projects)/(?P<team_id>\d+)/{endpoint}/?$"


GUEST_RULES: list[GuestRule] = [
    AlwaysAllowed(r"^/api/users/@me(/.*)?$"),
    AlwaysAllowed(r"^/api/organizations/@current/?$"),
    AlwaysAllowed(r"^/api/projects/@current/?$"),
    AlwaysAllowed(r"^/api/environments/@current/?$"),
    AlwaysAllowed(r"^/login/?$"),
    AlwaysAllowed(r"^/logout/?$"),
    AlwaysAllowed(r"^/api/login/?$"),
    AlwaysAllowed(r"^/api/logout/?$"),
    AlwaysAllowed(r"^/reset(/.*)?$"),
    AlwaysAllowed(r"^/signup/verify_email(/.*)?$"),
    AlwaysAllowed(r"^/_preflight/?$"),
    AlwaysAllowed(r"^/static/.*$"),
    AlwaysAllowed(r"^/favicon\.ico$"),
    AlwaysAllowed(r"^/guest(/.*)?$"),
    *[TeamScopedMetadataRead(_metadata_pattern(endpoint)) for endpoint in _METADATA_ENDPOINTS],
    # Anchored with `$` so sub-actions (e.g. `/dashboards/4/sharing/`, `/dashboards/4/collaborators/`)
    # do NOT inherit the grant — they need their own rule if we ever want to expose them.
    # Viewers don't need sub-actions; without this anchor a dashboard grant would leak the sharing
    # config and collaborator list for the granted dashboard.
    GrantBoundResource(
        "dashboard",
        r"^/api/(?:environments|projects)/(?P<team_id>\d+)/dashboards/(?P<resource_id>\d+)/?$",
    ),
    GrantBoundResource(
        "insight",
        r"^/api/(?:environments|projects)/(?P<team_id>\d+)/insights/(?P<resource_id>[A-Za-z0-9]+)/?$",
    ),
    GrantBoundResource(
        "notebook",
        r"^/api/(?:environments|projects)/(?P<team_id>\d+)/notebooks/(?P<resource_id>[A-Za-z0-9-]+)/?$",
    ),
    GrantBoundListFilter(
        "insight",
        r"^/api/(?:environments|projects)/(?P<team_id>\d+)/insights/?$",
    ),
    GrantBoundListFilter(
        "notebook",
        r"^/api/(?:environments|projects)/(?P<team_id>\d+)/notebooks/?$",
    ),
    SceneBoundQuery(r"^/api/(?:environments|projects)/(?P<team_id>\d+)/query(/[A-Z][A-Za-z]*)?/?$"),
]


class GuestDeflectionMiddleware:
    """Deflects guest users from every endpoint not allowed by a rule in `GUEST_RULES`.

    Placement: after `AuthenticationMiddleware` (needs `request.user`) but before
    `ActiveOrganizationMiddleware` — same slot family as impersonation middlewares.
    """

    def __init__(self, get_response):
        self.get_response = get_response
        self._rules: list[GuestRule] = GUEST_RULES

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not self._user_is_guest(request):
            return self.get_response(request)

        user = cast(User, request.user)
        for rule in self._rules:
            match = rule.matches(request)
            if match and rule.allows(request, user, match):
                return self.get_response(request)

        return self._deflect(request)

    def _user_is_guest(self, request: HttpRequest) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        return user.organization_memberships.filter(is_guest=True).exists()

    def _deflect(self, request: HttpRequest) -> HttpResponse:
        if request.path.startswith("/api/"):
            return JsonResponse({"detail": "Not found."}, status=404)
        # Defensive: /guest is covered by AlwaysAllowed above, but belt-and-suspenders —
        # returning 404 here prevents an infinite redirect loop if that rule is ever removed.
        if request.path.startswith("/guest"):
            return JsonResponse({"detail": "Not found."}, status=404)
        return redirect("/guest")
