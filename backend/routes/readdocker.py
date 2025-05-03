from fastapi import FastAPI, APIRouter, Response
from loguru import logger
from utils import vitals, setting, diffiehellman
import json
import os
import fastapi
import statics
import socket
import random
import string
import docker

from docker.models.containers import Container
from docker.models.volumes import Volume
from docker.models.networks import Network

client = docker.from_env()
router = APIRouter()

@router.get("/back/docker/container/{container_id}/info")
def container(request: fastapi.Request, container_id: str):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    container : Container = client.containers.get(container_id)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps(container.attrs))

@router.get("/back/docker/container/{container_id}/logs")
def logs(request: fastapi.Request, container_id: str):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    container : Container = client.containers.get(container_id)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], container.logs())

@router.get("/back/docker/container/{container_id}/top")
def top(request: fastapi.Request, container_id: str):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    container : Container = client.containers.get(container_id)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], container.top())

@router.get("/back/docker/container/{container_id}/stats")
def top(request: fastapi.Request, container_id: str):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    container : Container = client.containers.get(container_id)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], container.stats())

@router.get("/back/docker/container/{container_id}/start")
def container_start(request: fastapi.Request, container_id: str):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    container : Container = client.containers.get(container_id)
    container.start()
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], "YES")

@router.get("/back/docker/container/{container_id}/stop")
def container_stop(request: fastapi.Request, container_id: str):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    container : Container = client.containers.get(container_id)
    container.stop()
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], "YES")

@router.get("/back/docker/container/{container_id}/restart")
def container_restart(request: fastapi.Request, container_id: str):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    container : Container = client.containers.get(container_id)
    container.restart()
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], "YES")

@router.post("/back/docker/container/{container_id}/remove")
def container_remove(request: fastapi.Request, container_id: str):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    container : Container = client.containers.get(container_id)
    if(request.json()["image"] != container.image.short_id):
        return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], "NO")
    if(container.status == "running"): container.stop()
    container.remove()
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], "YES")

@router.get("/back/docker/volume/{volume_id}/info")
def volume(request: fastapi.Request, volume_id: str):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    volume : Volume = client.volumes.get(volume_id)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps(volume.attrs))

@router.get("/back/docker/network/{network_id}/info")
def network(request: fastapi.Request, network_id):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    network : Network = client.networks.get(network_id)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps(network.attrs))

@router.get("/back/docker/network/{network_id}/containers")
def networkcon(request: fastapi.Request, network_id):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    network : Network = client.networks.get(network_id)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({c.id: c.name for c in network.containers}))

