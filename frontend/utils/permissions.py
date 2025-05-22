from enum import Enum
from functools import cache

class Permission(Enum):
    FULL_ACCESS = "0"
    ALLOW_DEPLOYMENT = "1"
    ALLOW_DEPLOYMENT_MODIFICATIONS = "2"
    ALLOW_DEPLOYMENT_DELETE = "3"
    ALLOW_ADD_DOCKERS = "4"
    ALLOW_MODIFY_DOCKERS = "5"
    ALLOW_DELETE_DOCKERS = "6"
    ALLOW_SETTINGS = "7"
    ALLOW_RISKY_SETTINGS = "8"
    ALLOW_CREATING_USERS_SETTING = "9"
    ALLOW_MODIFYING_USERS = "A"
    ALLOW_DELETING_USERS = "B"
    ALLOW_PULL_IMAGES = "D"
    ALLOW_RISKY_PERMISSIONS = "C"

risky_permissions = [
    Permission.ALLOW_DEPLOYMENT_DELETE,
    Permission.ALLOW_DELETING_USERS,
    Permission.ALLOW_DELETE_DOCKERS,
    Permission.ALLOW_RISKY_PERMISSIONS,
    Permission.ALLOW_RISKY_SETTINGS
]

@cache
def string_to_permission(string: str) -> list[Permission]:
    try:
        return [Permission(char) for char in string if char.isdigit()]
    except ValueError as e:
        raise ValueError(f"Invalid permission string: {string}") from e