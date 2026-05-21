"""User model and role constants."""

from __future__ import annotations

from dataclasses import dataclass

# Role hierarchy: super_admin > admin > user
ROLE_SUPER_ADMIN = "super_admin"
ROLE_ADMIN = "admin"
ROLE_USER = "user"

ALL_ROLES = [ROLE_SUPER_ADMIN, ROLE_ADMIN, ROLE_USER]

# Which roles each role is allowed to create
CAN_CREATE_ROLES = {
    ROLE_SUPER_ADMIN: [ROLE_SUPER_ADMIN, ROLE_ADMIN, ROLE_USER],
    ROLE_ADMIN: [ROLE_USER],
    ROLE_USER: [],
}


@dataclass
class User:
    email: str
    profile_name: str
    role: str

    @property
    def can_configure_clients(self) -> bool:
        return self.role in (ROLE_SUPER_ADMIN, ROLE_ADMIN)

    @property
    def can_manage_users(self) -> bool:
        return self.role in (ROLE_SUPER_ADMIN, ROLE_ADMIN)

    @property
    def creatable_roles(self) -> list[str]:
        return CAN_CREATE_ROLES.get(self.role, [])
