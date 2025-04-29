import json
import os

from functools import cache

def settings_exists() -> bool:
    return os.path.exists(os.path.join(os.getcwd(), "settings.json"))

@cache
def read_settings() -> dict:
    if(settings_exists()):
        with open(os.path.join(os.getcwd(), "settings.json"), "r") as f:
            return json.loads(f.read())
    return {}
    
def update_settings(**kwargs) -> None:
    read = read_settings()
    for key, value in kwargs.items():
        read[key] = value
    with open(os.path.join(os.getcwd(), "settings.json"), "w") as f:
        f.write(json.dumps(read))
        
    read_settings.cache_clear()