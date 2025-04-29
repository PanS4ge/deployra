from utils import diffiehellman
from utils import settingsgen
import requests
import os
import statics
from functools import cache
import traceback
import uuid
import urllib
import json
from loguru import logger

def join_url(*segments) -> str:
    return '/'.join(segment.strip('/') for segment in segments)

def autofill_http(*segments) -> str:
    url = join_url(*segments).replace("%5C", "/").replace("//", "/")
    if url.startswith("http"):
        return url
    return "http://" + url

def check_backend(url) -> str:
    try:
        return requests.get(autofill_http(join_url(url, "back", "identify")))
    except Exception as e:
        #traceback.print_exc()
        return None

def save_backend(url, name, shared, access_key) -> bool:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        settingsgen.get_storage_type().create_db(statics.db_name + "_connections", uuid="STRING", url="STRING", name="TINYSTRING", shared="STRING", access_key="STRING", cpu_cores="INT", ram="TINYSTRING", rawram="BIGINT", created_at="TIMESTAMP CURRENT_TIMESTAMP()")
    vitals = json.loads(get_url_vitals(url, shared, access_key))
    return settingsgen.get_storage_type().save_value(statics.db_name + "_connections", str(uuid.uuid4()), url=url, name=name, shared=shared, access_key=access_key, cpu_cores=int(vitals["cpu"]), ram=vitals["ram"], rawram=int(vitals["rawram"]))

def connect_to_backend(uuid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return False
    try:
        return requests.get(autofill_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "identify")))
    except Exception as e:
        traceback.print_exc()
        return False

def get_url_vitals(url, shared, access) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(shared, requests.get(autofill_http(join_url(url, "back", "vitals", "amount")), headers={"Authorization": diffiehellman.encrypt_message_delivery(shared, access)}).text)

def get_vitals(uuid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "vitals", "ramusage")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

@cache
def get_available_vitals(uuid):
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    try:
        amounts = diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "vitals", "amount")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)
        cpucores = amounts["cpu"]
        ram = amounts["ram"]
        rawram = amounts["rawram"]
        cpucores_old = settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "cpu_cores")
        ram_old = settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "ram")
        rawram_old = settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "rawram")
        if(cpucores != cpucores_old or ram != ram_old or rawram != rawram_old):
            # change detected
            settingsgen.get_storage_type().save_value(statics.db_name + "_connections", uuid, cpu_cores=cpucores, ram=ram, rawram=rawram)
        return {
            "cpu": cpucores,
            "ram": ram,
            "rawram": rawram
        }
    except:
        return {
            "cpu": settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "cpu_cores"),
            "ram": settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "ram"),
            "rawram": settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "rawram")
        }

def set_name(uuid, name):
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return settingsgen.get_storage_type().save_value(statics.db_name + "_connections", uuid, name=name)

@cache
def exchange_dh(url) -> tuple[str, str]:
    dh = {
        "pub": diffiehellman.generate_public_key(settingsgen.read_cryptography()["diffiehellman_gen"], settingsgen.read_cryptography()["diffiehellman_prime"], settingsgen.read_cryptography()["diffiehellman_priv"]),
        "prime": settingsgen.read_cryptography()["diffiehellman_prime"],
        "gen": settingsgen.read_cryptography()["diffiehellman_gen"]
    }
    req = requests.post(autofill_http(join_url(url, "back", "exchange")), headers={"Content-Type": "application/json"}, json=dh)
    if(req.text == "404"):
        return None
    pub = req.json()["pub"]
    logger.info(f"Received public key from {url}")
    shared = diffiehellman.generate_shared_secret(pub, settingsgen.read_cryptography()["diffiehellman_priv"], settingsgen.read_cryptography()["diffiehellman_prime"])
    logger.info(f"Finished exchange with {url}")
    return (shared, req.json()["access"])