"""Central service for creating, deleting, and mirroring guest grants.

Every write path goes through here. Each `GuestResourceGrant` row is paired with an
`AccessControl` row at `access_level="viewer"` so the existing AC machinery resolves
the guest's access on granted resources without any guest-specific AC branches —
this is the "mirror" that lets us avoid patching `user_access_control.py` and friends.

Adding a new resource type means accepting it in the grant table and letting the
middleware's rule for that type fire; no code here needs changing unless the AC
`resource` string differs from the grant `resource` string.
"""

from typing import Any

from django.db import transaction

from rest_framework import exceptions

from posthog.constants import AvailableFeature
from posthog.models import GuestResourceGrant, OrganizationMembership
from posthog.models.activity_logging.activity_log import Change, Detail, log_activity
from posthog.models.insight import Insight
from posthog.models.organization import Organization
from posthog.models.team.team import Team
from posthog.models.user import User

from products.dashboards.backend.models.dashboard import Dashboard
from products.dashboards.backend.models.dashboard_tile import DashboardTile
from products.notebooks.backend.models import Notebook

from ee.models.rbac.access_control import AccessControl

VALID_RESOURCES: tuple[str, ...] = ("dashboard", "insight", "notebook")
GUEST_VIEWER_ACCESS_LEVEL = "viewer"


def _resource_exists_in_team(resource: str, resource_id: str, team_id: int) -> bool:
    """Does the grant target actually exist? URL identifiers differ by resource:

    - dashboard: stringified numeric PK
    - insight / notebook: `short_id`, but legacy numeric PK addressing is also allowed
    """
    value = str(resource_id)
    if resource == "dashboard":
        if not value.isdigit():
            return False
        return Dashboard.objects.filter(id=int(value), team_id=team_id).exists()
    model: Any
    if resource == "insight":
        model = Insight
    elif resource == "notebook":
        model = Notebook
    else:
        return False
    if value.isdigit() and model.objects.filter(id=int(value), team_id=team_id).exists():
        return True
    return model.objects.filter(short_id=value, team_id=team_id).exists()


def validate_invite_grants(organization: Organization, guest_resources: list[dict[str, Any]]) -> None:
    """Validate the shape and existence of each entry in an invite's `guest_resources`.

    Raises `ValidationError` with a caller-friendly message on the first failure.
    """
    if not organization.is_feature_available(AvailableFeature.ACCESS_CONTROL):
        raise exceptions.ValidationError(
            "Guest invites require the Advanced permissions feature. Upgrade to enable them."
        )

    if not guest_resources:
        raise exceptions.ValidationError("Guest invites must specify at least one resource grant.")

    org_team_ids = set(organization.teams.values_list("id", flat=True))

    for grant in guest_resources:
        team_id = grant.get("team_id")
        resource = grant.get("resource")
        resource_id = grant.get("resource_id")

        if team_id not in org_team_ids:
            raise exceptions.ValidationError(f"Team {team_id} does not belong to this organization.")
        if resource not in VALID_RESOURCES:
            raise exceptions.ValidationError(
                f"Invalid resource type '{resource}'. Must be one of: {', '.join(sorted(VALID_RESOURCES))}."
            )
        if resource_id is None or not _resource_exists_in_team(resource, str(resource_id), int(team_id)):
            raise exceptions.ValidationError(f"{resource.capitalize()} {resource_id} does not exist in team {team_id}.")


@transaction.atomic
def create_grant(
    *,
    membership: OrganizationMembership,
    team: Team,
    resource: str,
    resource_id: str,
    created_by: User,
) -> GuestResourceGrant:
    """Create a `GuestResourceGrant` plus its mirroring `AccessControl` row at viewer access."""
    if resource not in VALID_RESOURCES:
        raise exceptions.ValidationError(f"Invalid resource: {resource}")

    grant = GuestResourceGrant.objects.create(
        organization_membership=membership,
        team=team,
        resource=resource,
        resource_id=str(resource_id),
        created_by=created_by,
    )

    ac_resource_id = _ac_resource_id(resource, str(resource_id), team.id)
    if ac_resource_id is not None:
        AccessControl.objects.get_or_create(
            team=team,
            resource=resource,
            resource_id=ac_resource_id,
            organization_member=membership,
            role=None,
            defaults={"access_level": GUEST_VIEWER_ACCESS_LEVEL, "created_by": created_by},
        )
    # Dashboard grants cascade viewer AC to each tile insight at grant time so the
    # insight scene resolves user_access_level="viewer" naturally. Tiles added later
    # won't auto-propagate; accept as a v1 limitation.
    if resource == "dashboard":
        _cascade_ac_to_dashboard_tiles(
            team=team,
            dashboard_id=str(resource_id),
            membership=membership,
            created_by=created_by,
        )

    return grant


def _ac_resource_id(resource: str, grant_resource_id: str, team_id: int) -> str | None:
    """Guest grants use URL identifiers (numeric PK for dashboards, short_id for
    insights/notebooks), while the AC table uses the numeric PK for all resources.
    Translate before writing AC rows."""
    if resource == "dashboard":
        return grant_resource_id if grant_resource_id.isdigit() else None
    model: Any
    if resource == "insight":
        model = Insight
    elif resource == "notebook":
        model = Notebook
    else:
        return grant_resource_id
    if grant_resource_id.isdigit():
        return grant_resource_id
    pk = model.objects.filter(short_id=grant_resource_id, team_id=team_id).values_list("id", flat=True).first()
    return str(pk) if pk is not None else None


def _cascade_ac_to_dashboard_tiles(
    *, team: Team, dashboard_id: str, membership: OrganizationMembership, created_by: User
) -> None:
    if not dashboard_id.isdigit():
        return
    tile_insight_pks = DashboardTile.objects.filter(dashboard_id=int(dashboard_id), insight__isnull=False).values_list(
        "insight_id", flat=True
    )
    for pk in tile_insight_pks:
        if pk is None:
            continue
        AccessControl.objects.get_or_create(
            team=team,
            resource="insight",
            resource_id=str(pk),
            organization_member=membership,
            role=None,
            defaults={"access_level": GUEST_VIEWER_ACCESS_LEVEL, "created_by": created_by},
        )


@transaction.atomic
def delete_grant(grant: GuestResourceGrant) -> None:
    """Delete a grant and the AC rows that mirror it."""
    ac_resource_id = _ac_resource_id(grant.resource, grant.resource_id, grant.team_id)
    if ac_resource_id is not None:
        AccessControl.objects.filter(
            team=grant.team,
            resource=grant.resource,
            resource_id=ac_resource_id,
            organization_member_id=grant.organization_membership_id,
        ).delete()
    if grant.resource == "dashboard" and grant.resource_id.isdigit():
        tile_insight_pks = DashboardTile.objects.filter(
            dashboard_id=int(grant.resource_id), insight__isnull=False
        ).values_list("insight_id", flat=True)
        AccessControl.objects.filter(
            team=grant.team,
            resource="insight",
            resource_id__in=[str(pk) for pk in tile_insight_pks if pk is not None],
            organization_member_id=grant.organization_membership_id,
        ).delete()
    grant.delete()


@transaction.atomic
def apply_invite_grants(
    invite: "Any",  # OrganizationInvite — annotated as Any to avoid a circular import
    new_membership: OrganizationMembership,
) -> list[GuestResourceGrant]:
    """Materialize an invite's `guest_resources` into active grants for a new membership."""
    created: list[GuestResourceGrant] = []
    for entry in invite.guest_resources or []:
        team = Team.objects.get(id=entry["team_id"])
        created.append(
            create_grant(
                membership=new_membership,
                team=team,
                resource=entry["resource"],
                resource_id=str(entry["resource_id"]),
                created_by=invite.created_by,
            )
        )
    return created


@transaction.atomic
def promote_to_member(membership: OrganizationMembership, by: User) -> int:
    """Convert a guest membership into a regular member.

    Deletes all grants + mirroring AC rows and flips `is_guest` to False.
    Returns the number of grants removed.
    """
    if not membership.is_guest:
        raise exceptions.ValidationError("This membership is already a regular member.")

    grants = list(GuestResourceGrant.objects.filter(organization_membership=membership))
    removed = len(grants)
    for grant in grants:
        delete_grant(grant)

    # Reset SSO bypass on promotion — the carve-out was granted for a guest scenario;
    # elevating to full member should require re-granting if the admin still wants it.
    membership.is_guest = False
    membership.bypass_sso = False
    membership.save(update_fields=["is_guest", "bypass_sso", "updated_at"])

    log_activity(
        organization_id=membership.organization_id,
        team_id=None,
        user=by,
        was_impersonated=False,
        item_id=membership.id,
        scope="OrganizationMembership",
        activity="promoted_from_guest",
        detail=Detail(
            name=membership.user.email if membership.user else None,
            changes=[
                Change(
                    type="OrganizationMembership",
                    action="changed",
                    field="is_guest",
                    before=True,
                    after=False,
                )
            ],
        ),
    )

    return removed
