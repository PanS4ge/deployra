from utils import diffiehellman
from utils import settingsgen
import colorama
import requests
import os
import statics
from functools import cache
import traceback
import uuid
import urllib
import json
from utils.caching.ttl_cache import ttl_cache
from utils.caching.local_cache import local_cache
from loguru import logger

def join_url(*segments) -> str:
    return '/'.join(segment.strip('/') for segment in segments)

def autofill_and_log_http(*segments) -> str:
    url = join_url(*segments).replace("%5C", "/").replace("//", "/")
    
    color = colorama.Fore.LIGHTMAGENTA_EX
    if("remove" in url):
        color = colorama.Fore.LIGHTRED_EX
    elif("create" in url or "pull" in url):
        color = colorama.Fore.LIGHTGREEN_EX
    elif("info" in url):
        color = colorama.Fore.LIGHTCYAN_EX
    logger.debug(f"{colorama.Fore.YELLOW}Sending request to {color}{url}{colorama.Fore.RESET}")

    if url.startswith("http"):
        return url
    return "http://" + url

def check_backend(url) -> str:
    try:
        return requests.get(autofill_and_log_http(join_url(url, "back", "identify")))
    except Exception as e:
        #traceback.print_exc()
        return None

def save_backend(url, name, ipad, shared, access_key) -> bool:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        settingsgen.get_storage_type().create_db(statics.db_name + "_connections", uuid="STRING", url="STRING", name="TINYSTRING", ipaddr="STRING", shared="STRING", access_key="STRING", cpu_cores="INT", ram="TINYSTRING", rawram="BIGINT", servername="TINYSTRING", serverversion="TINYSTRING", hostname="TINYSTRING", created_at="TIMESTAMP CURRENT_TIMESTAMP()")
    vitals = json.loads(get_url_vitals(url, shared, access_key))
    ident = check_backend(url).json()
    if(ident is None):
        return False
    sname = ident["id"].split(" ")[0]
    sver = ident["id"].split(" ")[1]
    host = ident["host"]
    uuidd = str(uuid.uuid4())
    return uuidd if settingsgen.get_storage_type().save_value(statics.db_name + "_connections", uuidd, url=url, name=name, ipaddr=ipad, shared=shared, access_key=access_key, cpu_cores=int(vitals["cpu"]), ram=vitals["ram"], rawram=int(vitals["rawram"]), servername=sname, serverversion=sver, hostname=host) else "False"

def get_all_connections() -> list:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return []
    return settingsgen.get_storage_type().list_keys(statics.db_name + "_connections")

def connect_to_backend(uuid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return False
    try:
        return requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "identify")))
    except Exception as e:
        traceback.print_exc()
        return False

def free_connection(uuid) -> bool:    
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return False
    r = requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "remove")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))})
    if(r != 200):
        return False
    return settingsgen.get_storage_type().delete_value(statics.db_name + "_connections", uuid)

def get_url_vitals(url, shared, access) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(shared, requests.get(autofill_and_log_http(join_url(url, "back", "vitals", "amount")), headers={"Authorization": diffiehellman.encrypt_message_delivery(shared, access)}).text)

def get_vitals(uuid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "vitals", "usage")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def get_all_cpu_cores(uuid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "vitals", "cpuusages")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

@cache
def get_available_vitals(uuid):
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    try:
        amounts = diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "vitals", "amount")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)
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
    req = requests.post(autofill_and_log_http(join_url(url, "back", "exchange")), headers={"Content-Type": "application/json"}, json=dh)
    if(req.status_code == 404):
        return None
    pub = req.json()["pub"]
    logger.info(f"Received public key from {url}")
    shared = diffiehellman.generate_shared_secret(pub, settingsgen.read_cryptography()["diffiehellman_priv"], settingsgen.read_cryptography()["diffiehellman_prime"])
    logger.info(f"Finished exchange with {url}")
    return (shared, req.json()["access"])

def save_ip(uuid, ip):
    r = requests.post(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "saveip")), json={"ip": ip}, headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))})
    if(r != 200):
        return False
    else:
        return True





def get_images(uuid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "images")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def get_containers(uuid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "containers")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def get_container_statuses(uuid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "containers", "statuses")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def get_volumes(uuid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "volumes")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def get_networks(uuid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "networks")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)


@local_cache
def container_info(uuid, containerid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "container", containerid, "info")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def container_logs(uuid, containerid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "container", containerid, "logs")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def container_top(uuid, containerid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "container", containerid, "top")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def container_stats(uuid, containerid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "container", containerid, "stats")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def container_start(uuid, containerid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "container", containerid, "start")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def container_stop(uuid, containerid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "container", containerid, "stop")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def container_restart(uuid, containerid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "container", containerid, "restart")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def container_remove(uuid, containerid, image) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.post(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "container", containerid, "restart")), json={"image": image}, headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def volume_info(uuid, volumeid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "volume", volumeid, "info")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def network_info(uuid, networkid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "network", networkid, "info")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def containers_by_network(uuid, networkid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "network", networkid, "containers")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)



def pull_image(uuid, **pullargs) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.post(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "image", "pull")), json=pullargs, headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def prune_image(uuid, **pullargs) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.post(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "image", "prune")), json=pullargs, headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def container_deploy(uuid, **deployargs) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.post(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "container", "deploy")), json=deployargs, headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def volume_create(uuid, **volargs) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.post(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "volume", "create")), json=volargs, headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def volume_remove(uuid, **volargs) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.post(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "volume", "remove")), json=volargs, headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def network_create(uuid, **netargs) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.post(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "network", "create")), json=netargs, headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def network_remove(uuid, **netargs) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.post(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "network", "remove")), json=netargs, headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def image_remove(uuid, **imageargs) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.post(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "docker", "image", "remove")), json=imageargs, headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)


def get_nginx(uuid) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.get(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "nginx")), headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def create_nginx(uuid, domain, target_container_name, target_container_port, ssl, ssl_email) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    nginxd = {
        "domain": domain,
        "target_container_name": target_container_name,
        "target_container_port": target_container_port,
        "ssl": ssl,
        "ssl_email": ssl_email
    }
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.post(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "nginx", "create")), json=nginxd, headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def delete_nginx(uuid, domain) -> str:
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    nginxd = {
        "domain": domain
    }
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.post(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "nginx", "delete")), json=nginxd, headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)

def create_ftp(uuid, **kwargs):
    if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
        return
    return diffiehellman.decrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), requests.post(autofill_and_log_http(join_url(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "url"), "back", "nginx", "delete")), json=kwargs, headers={"Authorization": diffiehellman.encrypt_message_delivery(settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "shared"), settingsgen.get_storage_type().get_value(statics.db_name + "_connections", uuid, "access_key"))}).text)
 