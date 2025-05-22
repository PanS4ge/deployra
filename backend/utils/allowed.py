from fastapi import Request, Response
from utils import setting, diffiehellman

def is_request_allowed(request: Request) -> bool:
    if(not setting.settings_exists()): return False
    if not("authorization" in request.headers.keys()):
        return False
    try:
        if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
            return False
    except:
        return False
    return True