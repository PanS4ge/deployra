import hashlib
from utils import settingsgen
import statics
from urllib.parse import urlencode
from functools import cache

@cache
def get_gravatar(email : str, size : int = 40) -> str:
    default = ""
    if(settingsgen.are_there_settings()):
        default = settingsgen.read_settings().get("gravatardefault")
    else:
        default = statics.gravatar_default_default
    
    email_encoded = email.lower().encode('utf-8')
    
    email_hash = hashlib.sha256(email_encoded).hexdigest()
    
    query_params = urlencode({'d': default, 's': str(size)})
    gravatar_url = f"https://www.gravatar.com/avatar/{email_hash}?{query_params}"
    return gravatar_url