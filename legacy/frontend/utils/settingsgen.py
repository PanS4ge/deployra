from utils.languages.languagefile import Language
from utils.providers.dnsprovider import DNSProvider
import importlib
import statics
import json
import os
from loguru import logger
from functools import cache
from typing import Union, Any
from utils.storage.storage import Storage

def generate_cryptography(**kwargs) -> None:
    if(not os.path.exists(os.path.join(os.getcwd(), "cryptography.json"))):
        with open(os.path.join(os.getcwd(), "cryptography.json"), "w") as f:
            f.write(json.dumps(kwargs))
        
@cache
def read_cryptography() -> dict:
    with open(os.path.join(os.getcwd(), "cryptography.json"), "r") as f:
        return json.loads(f.read())
    
def generate_settings(**kwargs) -> None:
    if(not os.path.exists(os.path.join(os.getcwd(), "settings.json"))):
        with open(os.path.join(os.getcwd(), "settings.json"), "w") as f:
            f.write(json.dumps(kwargs))

@cache
def read_settings() -> dict:
    with open(os.path.join(os.getcwd(), "settings.json"), "r") as f:
        return json.loads(f.read())

def update_settings(**kwargs) -> None:
    read = read_settings()
    for key, value in kwargs.items():
        read[key] = value
    with open(os.path.join(os.getcwd(), "settings.json"), "w") as f:
        f.write(json.dumps(read))
    read_settings.cache_clear()
    
def update_cryptography(**kwargs) -> None:
    read = read_cryptography()
    for key, value in kwargs.items():
        read[key] = value
    with open(os.path.join(os.getcwd(), "cryptography.json"), "w") as f:
        f.write(json.dumps(read))
    read_cryptography.cache_clear()

def are_there_settings() -> bool:
    return os.path.exists(os.path.join(os.getcwd(), "settings.json"))

@cache
def get_language():
    try:
        lang_name = ""
        if(are_there_settings()):
            lang_name = read_settings().get("language")
        else:
            lang_name = statics.default_language
        module = importlib.import_module(f"utils.languages.{lang_name.lower()}")
        language_class = getattr(module, lang_name)
        language_instance = language_class()
        logger.debug(f"Using language {language_instance.__class__}")
        return language_instance
    except Exception as e:
        logger.error(f"Failed to load language: {e}")
        fallback_module = importlib.import_module(f"utils.languages.{statics.default_language.lower()}")
        fallback_class = getattr(fallback_module, statics.default_language)
        return fallback_class()
    
@cache
def get_storage_type() -> Union[Storage, None]:
    if(not are_there_settings()):
        return None
    module = importlib.import_module(f"utils.storage.{read_settings().get('usestorage').replace(' ', '_').lower()}")
    storage_class = getattr(module, read_settings().get("usestorage").replace(' ', '_').upper())
    logger.debug(f"Using storage {storage_class().__class__}")
    return storage_class()

@cache
def get_dns_provider() -> Union[DNSProvider, None]:
    if(not are_there_settings()):
        return None
    module = importlib.import_module(f"utils.providers.{read_settings().get('dnsprovider').replace(' ', '_').lower()}")
    dnsprovider_class = getattr(module, read_settings().get("dnsprovider").replace(' ', '_').upper())
    logger.debug(f"Using DNS provider {dnsprovider_class().__class__}")
    return dnsprovider_class()