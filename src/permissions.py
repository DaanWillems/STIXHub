from models.domain import RoleConfig


def get_effective_permissions(
    role_names: list[str], role_configs: list[RoleConfig]
) -> tuple[set[str], set[str]]:
    """Return the bucket names readable and writable by the union of ``role_names``.

    Unknown role names are ignored.
    """
    can_read: set[str] = set()
    can_write: set[str] = set()

    role_map = {role.name: role for role in role_configs}

    for role_name in role_names:
        if role := role_map.get(role_name):
            can_read.update(role.can_read)
            can_write.update(role.can_write)
    return can_read, can_write
