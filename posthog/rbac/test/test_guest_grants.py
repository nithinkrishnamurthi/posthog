from posthog.test.base import BaseTest

from rest_framework import exceptions

from posthog.constants import AvailableFeature
from posthog.models import GuestResourceGrant, OrganizationMembership
from posthog.models.user import User
from posthog.rbac.guest_grants import (
    GUEST_VIEWER_ACCESS_LEVEL,
    apply_invite_grants,
    create_grant,
    delete_grant,
    promote_to_member,
    validate_invite_grants,
)

from products.dashboards.backend.models.dashboard import Dashboard

from ee.models.rbac.access_control import AccessControl


class TestGuestGrants(BaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.organization.available_product_features = [
            {"key": AvailableFeature.ACCESS_CONTROL, "name": "Access control"}
        ]
        self.organization.save()

        self.guest_user = User.objects.create_user(
            email="guest@example.com", first_name="Guest", password="password123"
        )
        self.guest_membership = OrganizationMembership.objects.create(
            organization=self.organization, user=self.guest_user, is_guest=True
        )
        self.dashboard = Dashboard.objects.create(team=self.team, name="Dash")

    def test_create_grant_mirrors_an_access_control_row(self) -> None:
        grant = create_grant(
            membership=self.guest_membership,
            team=self.team,
            resource="dashboard",
            resource_id=str(self.dashboard.pk),
            created_by=self.user,
        )

        self.assertEqual(grant.resource, "dashboard")
        self.assertEqual(grant.resource_id, str(self.dashboard.pk))
        self.assertFalse(grant.is_pending)

        ac = AccessControl.objects.get(
            team=self.team,
            resource="dashboard",
            resource_id=str(self.dashboard.pk),
            organization_member=self.guest_membership,
        )
        self.assertEqual(ac.access_level, GUEST_VIEWER_ACCESS_LEVEL)

    def test_pending_grant_does_not_create_access_control_row(self) -> None:
        create_grant(
            membership=self.guest_membership,
            team=self.team,
            resource="dashboard",
            resource_id=str(self.dashboard.pk),
            created_by=self.user,
            is_pending=True,
        )
        self.assertFalse(
            AccessControl.objects.filter(
                team=self.team,
                resource="dashboard",
                resource_id=str(self.dashboard.pk),
            ).exists()
        )

    def test_delete_grant_removes_access_control_row(self) -> None:
        grant = create_grant(
            membership=self.guest_membership,
            team=self.team,
            resource="dashboard",
            resource_id=str(self.dashboard.pk),
            created_by=self.user,
        )
        delete_grant(grant)

        self.assertFalse(GuestResourceGrant.objects.filter(pk=grant.pk).exists())
        self.assertFalse(
            AccessControl.objects.filter(
                team=self.team,
                resource="dashboard",
                resource_id=str(self.dashboard.pk),
                organization_member=self.guest_membership,
            ).exists()
        )

    def test_promote_to_member_removes_grants_and_flips_flag(self) -> None:
        create_grant(
            membership=self.guest_membership,
            team=self.team,
            resource="dashboard",
            resource_id=str(self.dashboard.pk),
            created_by=self.user,
        )
        other_dashboard = Dashboard.objects.create(team=self.team, name="Other")
        create_grant(
            membership=self.guest_membership,
            team=self.team,
            resource="dashboard",
            resource_id=str(other_dashboard.pk),
            created_by=self.user,
        )

        removed = promote_to_member(self.guest_membership, by=self.user)
        self.guest_membership.refresh_from_db()

        self.assertEqual(removed, 2)
        self.assertFalse(self.guest_membership.is_guest)
        self.assertFalse(GuestResourceGrant.objects.filter(organization_membership=self.guest_membership).exists())
        self.assertFalse(
            AccessControl.objects.filter(
                organization_member=self.guest_membership,
                resource="dashboard",
            ).exists()
        )

    def test_promote_to_member_rejects_non_guest(self) -> None:
        regular = OrganizationMembership.objects.get(organization=self.organization, user=self.user)
        with self.assertRaises(exceptions.ValidationError):
            promote_to_member(regular, by=self.user)

    def test_apply_invite_grants_creates_rows_for_each_entry(self) -> None:
        class _FakeInvite:
            guest_resources = [
                {"team_id": self.team.pk, "resource": "dashboard", "resource_id": str(self.dashboard.pk)},
            ]
            created_by = self.user

        created = apply_invite_grants(_FakeInvite(), self.guest_membership)
        self.assertEqual(len(created), 1)
        self.assertTrue(
            GuestResourceGrant.objects.filter(
                organization_membership=self.guest_membership,
                resource="dashboard",
                resource_id=str(self.dashboard.pk),
                is_pending=False,
            ).exists()
        )
        self.assertTrue(
            AccessControl.objects.filter(
                organization_member=self.guest_membership,
                resource="dashboard",
                resource_id=str(self.dashboard.pk),
            ).exists()
        )

    def test_validate_invite_grants_requires_access_control_feature(self) -> None:
        self.organization.available_product_features = []
        self.organization.save()
        with self.assertRaises(exceptions.ValidationError):
            validate_invite_grants(
                self.organization,
                [{"team_id": self.team.pk, "resource": "dashboard", "resource_id": str(self.dashboard.pk)}],
            )

    def test_validate_invite_grants_rejects_unknown_team(self) -> None:
        with self.assertRaises(exceptions.ValidationError):
            validate_invite_grants(
                self.organization,
                [{"team_id": 99999, "resource": "dashboard", "resource_id": str(self.dashboard.pk)}],
            )

    def test_validate_invite_grants_rejects_unknown_resource_type(self) -> None:
        with self.assertRaises(exceptions.ValidationError):
            validate_invite_grants(
                self.organization,
                [{"team_id": self.team.pk, "resource": "experiment", "resource_id": "1"}],
            )

    def test_validate_invite_grants_rejects_missing_resource_id(self) -> None:
        with self.assertRaises(exceptions.ValidationError):
            validate_invite_grants(
                self.organization,
                [{"team_id": self.team.pk, "resource": "dashboard", "resource_id": "99999"}],
            )

    def test_validate_invite_grants_requires_at_least_one_entry(self) -> None:
        with self.assertRaises(exceptions.ValidationError):
            validate_invite_grants(self.organization, [])
