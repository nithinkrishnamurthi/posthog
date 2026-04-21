from posthog.test.base import APIBaseTest

from parameterized import parameterized
from rest_framework import status

from posthog.models import GuestResourceGrant, OrganizationMembership
from posthog.models.insight import Insight
from posthog.models.user import User

from products.dashboards.backend.models.dashboard import Dashboard
from products.dashboards.backend.models.dashboard_tile import DashboardTile


class TestGuestDeflectionMiddleware(APIBaseTest):
    """Parameterized coverage of the guest deflection rule table.

    Each row describes a single request and its expected HTTP status. Setup wires up:
    - one guest user with an optional grant list
    - one granted dashboard + one granted notebook + one "tile" insight
    - one ungranted dashboard / insight / notebook as foils
    """

    def setUp(self) -> None:
        super().setUp()
        # Promote the base test user to an admin and create a separate guest user.
        OrganizationMembership.objects.filter(organization=self.organization, user=self.user).update(
            level=OrganizationMembership.Level.ADMIN
        )

        self.guest_user = User.objects.create_user(
            email="guest@example.com", first_name="Guest", password="password123"
        )
        self.guest_membership = OrganizationMembership.objects.create(
            organization=self.organization, user=self.guest_user, is_guest=True
        )

        self.granted_dashboard = Dashboard.objects.create(team=self.team, name="Granted dashboard")
        self.ungranted_dashboard = Dashboard.objects.create(team=self.team, name="Ungranted dashboard")
        self.ungranted_insight = Insight.objects.create(team=self.team, name="Ungranted insight")
        self.tile_insight = Insight.objects.create(team=self.team, name="Tile insight")
        DashboardTile.objects.create(dashboard=self.granted_dashboard, insight=self.tile_insight)

    def _login_guest(self) -> None:
        self.client.force_login(self.guest_user)

    def _login_regular(self) -> None:
        self.client.force_login(self.user)

    def _grant(self, resource: str, resource_id: str) -> GuestResourceGrant:
        # Mirror what `guest_grants.create_grant` does, minus the AC row — the middleware only
        # reads GuestResourceGrant, so tests keep setup lean. Service-level tests verify the AC
        # mirror side of the write path in `test_guest_grants.py`.
        return GuestResourceGrant.objects.create(
            organization_membership=self.guest_membership,
            team=self.team,
            resource=resource,
            resource_id=resource_id,
            is_pending=False,
        )

    def test_non_guest_user_is_never_deflected(self) -> None:
        self._login_regular()
        res = self.client.get(f"/api/projects/{self.team.pk}/dashboards/{self.ungranted_dashboard.pk}/")
        # Regular admin can read any dashboard they have AC on; the middleware doesn't deflect them.
        self.assertNotEqual(res.status_code, 404)

    def test_guest_user_without_grants_is_deflected_from_api(self) -> None:
        self._login_guest()
        res = self.client.get(f"/api/projects/{self.team.pk}/dashboards/{self.ungranted_dashboard.pk}/")
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_guest_user_without_grants_is_redirected_on_spa_route(self) -> None:
        self._login_guest()
        res = self.client.get(f"/project/{self.team.pk}/experiments", follow=False)
        self.assertEqual(res.status_code, status.HTTP_302_FOUND)
        self.assertEqual(res["Location"], "/guest")

    def test_guest_landing_page_is_not_deflected(self) -> None:
        self._login_guest()
        res = self.client.get("/guest", follow=False)
        # Route exists in frontend only, but middleware must not redirect; Django should reach
        # the SPA/catch-all handler. We only care that the status is not a 302 to /guest.
        self.assertNotEqual(res.status_code, status.HTTP_302_FOUND)

    def test_guest_with_dashboard_grant_can_read_granted_dashboard(self) -> None:
        self._grant("dashboard", str(self.granted_dashboard.pk))
        self._login_guest()
        res = self.client.get(f"/api/projects/{self.team.pk}/dashboards/{self.granted_dashboard.pk}/")
        self.assertNotEqual(res.status_code, 404)

    def test_guest_with_dashboard_grant_cannot_read_other_dashboard(self) -> None:
        self._grant("dashboard", str(self.granted_dashboard.pk))
        self._login_guest()
        res = self.client.get(f"/api/projects/{self.team.pk}/dashboards/{self.ungranted_dashboard.pk}/")
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_query_with_matching_scene_header_is_allowed(self) -> None:
        self._grant("dashboard", str(self.granted_dashboard.pk))
        self._login_guest()
        res = self.client.post(
            f"/api/projects/{self.team.pk}/query/",
            data={"query": {"kind": "HogQLQuery", "query": "SELECT 1"}},
            content_type="application/json",
            HTTP_X_POSTHOG_SCENE_RESOURCE=f"dashboard:{self.granted_dashboard.pk}",
        )
        self.assertNotEqual(res.status_code, 404)

    def test_query_with_mismatched_scene_header_is_deflected(self) -> None:
        self._grant("dashboard", str(self.granted_dashboard.pk))
        self._login_guest()
        res = self.client.post(
            f"/api/projects/{self.team.pk}/query/",
            data={"query": {"kind": "HogQLQuery", "query": "SELECT 1"}},
            content_type="application/json",
            HTTP_X_POSTHOG_SCENE_RESOURCE=f"dashboard:{self.ungranted_dashboard.pk}",
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_query_without_scene_header_is_deflected(self) -> None:
        self._grant("dashboard", str(self.granted_dashboard.pk))
        self._login_guest()
        res = self.client.post(
            f"/api/projects/{self.team.pk}/query/",
            data={"query": {"kind": "HogQLQuery", "query": "SELECT 1"}},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_query_with_malformed_scene_header_is_deflected(self) -> None:
        self._grant("dashboard", str(self.granted_dashboard.pk))
        self._login_guest()
        res = self.client.post(
            f"/api/projects/{self.team.pk}/query/",
            data={"query": {"kind": "HogQLQuery", "query": "SELECT 1"}},
            content_type="application/json",
            HTTP_X_POSTHOG_SCENE_RESOURCE="garbage-no-colon",
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_themes_metadata_is_allowed_for_guest_with_any_grant(self) -> None:
        self._grant("dashboard", str(self.granted_dashboard.pk))
        self._login_guest()
        res = self.client.get(f"/api/projects/{self.team.pk}/data_color_themes/")
        self.assertNotEqual(res.status_code, 404)

    def test_themes_metadata_post_is_deflected(self) -> None:
        self._grant("dashboard", str(self.granted_dashboard.pk))
        self._login_guest()
        res = self.client.post(
            f"/api/projects/{self.team.pk}/data_color_themes/",
            data={},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_tile_insight_is_allowed_when_parent_dashboard_is_granted(self) -> None:
        self._grant("dashboard", str(self.granted_dashboard.pk))
        self._login_guest()
        res = self.client.get(
            f"/api/projects/{self.team.pk}/insights/",
            {"short_id": self.tile_insight.short_id},
        )
        self.assertNotEqual(res.status_code, 404)

    def test_non_tile_insight_is_deflected_with_only_dashboard_grant(self) -> None:
        self._grant("dashboard", str(self.granted_dashboard.pk))
        self._login_guest()
        res = self.client.get(
            f"/api/projects/{self.team.pk}/insights/",
            {"short_id": self.ungranted_insight.short_id},
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_users_me_is_always_allowed(self) -> None:
        self._login_guest()
        res = self.client.get("/api/users/@me/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_organizations_current_is_always_allowed(self) -> None:
        self._login_guest()
        res = self.client.get("/api/organizations/@current/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    @parameterized.expand(
        [
            # Each endpoint is only registered on one of the routers (projects vs environments).
            # We pair the endpoint with the scope where Django actually has a URL pattern —
            # otherwise a regular Django 404 is indistinguishable from a middleware 404.
            ("annotations", "projects"),
            ("cohorts", "projects"),
            ("tags", "projects"),
            ("insight_variables", "environments"),
            ("quick_filters", "environments"),
            ("data_color_themes", "environments"),
        ]
    )
    def test_metadata_endpoints_require_any_grant(self, endpoint: str, scope: str) -> None:
        # No grants yet — middleware should deflect even though the endpoint is in the
        # metadata allowlist (guests with zero grants have no business in team metadata).
        self._login_guest()
        url = f"/api/{scope}/{self.team.pk}/{endpoint}/"
        res_without_grant = self.client.get(url)
        self.assertEqual(res_without_grant.status_code, status.HTTP_404_NOT_FOUND)

        self._grant("dashboard", str(self.granted_dashboard.pk))
        res_with_grant = self.client.get(url)
        self.assertNotEqual(res_with_grant.status_code, 404)
