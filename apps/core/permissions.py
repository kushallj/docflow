from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import SAFE_METHODS, BasePermission

from .models import Membership


def membership(user) -> Membership:
    m = getattr(user, "membership", None) if user and user.is_authenticated else None
    if m is None:
        raise PermissionDenied("Your account is not linked to an organisation")
    return m


class IsMember(BasePermission):
    """Any member can read; writes depend on the view's ``write_roles``."""

    def has_permission(self, request, view):
        m = membership(request.user)
        if request.method in SAFE_METHODS:
            return True
        roles = getattr(view, "write_roles", (Membership.Role.ADMIN,))
        action_roles = getattr(view, "action_roles", {}).get(getattr(view, "action", None))
        return m.role in (action_roles or roles)


class OrgScopedMixin:
    """Restrict a viewset's queryset to the caller's organisation."""

    permission_classes = [IsMember]

    @property
    def org(self):
        return membership(self.request.user).organization

    def get_queryset(self):
        return super().get_queryset().filter(organization=self.org)
