from datetime import datetime
from utils import settingsgen
import statics
import time

def add_notification(text : str):
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_notifications")):
        settingsgen.get_storage_type().create_db(statics.db_name + "_notifications", text="STRING", created_at="TIMESTAMP CURRENT_TIMESTAMP()")
    settingsgen.get_storage_type().save_value(statics.db_name + "_notifications", str(time.time()), text=text, created_at=datetime.now())

def read_notifications() -> list[str]:
    if(not settingsgen.are_there_settings()):
        return []
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_notifications")):
        settingsgen.get_storage_type().create_db(statics.db_name + "_notifications", text="STRING", created_at="TIMESTAMP CURRENT_TIMESTAMP()")
    keys = settingsgen.get_storage_type().list_keys(statics.db_name + "_notifications")
    if(keys is None): return []
    keys = sorted(keys, reverse=True)
    return [settingsgen.get_storage_type().get_value(statics.db_name + "_notifications", keys[i], "text") for i in range(min(len(keys), statics.max_notifications))]