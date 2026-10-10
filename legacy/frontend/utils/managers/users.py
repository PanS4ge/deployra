import statics
from utils import settingsgen
from utils import permissions
import hashlib
from functools import cache

def save_user(user : str, password : str, name : str, surname : str, permissions : str, undeletable : bool = False) -> bool:
    if not(settingsgen.get_storage_type().does_db_exist(statics.db_name + "_users")):
        settingsgen.get_storage_type().create_db(statics.db_name + "_users", user="TINYSTRING", password="STRING", name="TINYSTRING", surname="TINYSTRING", permissions="TINYSTRING", undeletable="BOOL", created_at="TIMESTAMP CURRENT_TIMESTAMP()")
    try:
        if (is_user(user)): return False
    except:
        pass
    return settingsgen.get_storage_type().save_value(statics.db_name + "_users", user, password=hashlib.sha256(password.encode('utf-8')).hexdigest(), name=name, surname=surname, permissions=permissions, undeletable=undeletable)

def is_deletable(user : str) -> bool:
    return not settingsgen.get_storage_type().get_value(statics.db_name + "_users", user, "undeletable")

def change_permissions(user : str, permissions : str) -> bool:
    return settingsgen.get_storage_type().update_value(statics.db_name + "_users", user, permissions=permissions)

def get_permissions(user : str) -> str:
    return settingsgen.get_storage_type().get_value(statics.db_name + "_users", user, "permissions")

def is_user(user : str) -> bool:
    return settingsgen.get_storage_type().get_value(statics.db_name + "_users", user, "password") is not None

def is_password_ok(user : str, password : str) -> bool:
    return settingsgen.get_storage_type().get_value(statics.db_name + "_users", user, "password") == hashlib.sha256(password.encode('utf-8')).hexdigest()

def get_field(user : str, field : str) -> str:
    return settingsgen.get_storage_type().get_value(statics.db_name + "_users", user, field)

def modify_user(user : str, changes : dict) -> bool:
    return settingsgen.get_storage_type().update_value(statics.db_name + "_users", user, **changes)

@cache
def get_name(user : str) -> str:
    return settingsgen.get_storage_type().get_value(statics.db_name + "_users", user, "name") + " " + settingsgen.get_storage_type().get_value(statics.db_name + "_users", user, "surname")

def has_raw_permission(user : str, rank : permissions.Permission) -> bool:
    stpr = permissions.string_to_permission(get_permissions(user))
    return rank in stpr

def has_permission(user : str, rank : permissions.Permission) -> bool:
    stpr = permissions.string_to_permission(get_permissions(user))
    return rank in stpr or permissions.Permission.FULL_ACCESS in stpr

def delete_user(user : str) -> bool:
    if(not is_deletable(user)): return False
    return settingsgen.get_storage_type().delete_value(statics.db_name + "_users", user)

def get_all_users() -> list[str]:
    return settingsgen.get_storage_type().list_keys(statics.db_name + "_users")
