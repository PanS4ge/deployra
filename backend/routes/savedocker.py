from fastapi import FastAPI, APIRouter, Response
from loguru import logger
from utils import vitals, setting, diffiehellman
import json
import os
import fastapi
import docker

from docker.models.containers import Container
from docker.models.volumes import Volume
from docker.models.networks import Network

client = docker.from_env()
router = APIRouter()

@router.post("/back/docker/image/pull")
def images(request: fastapi.Request):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    r = request.json()
    image = client.images.pull(**r)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({image.id: image.tags[0]}))

@router.post("/back/docker/image/prune")
def images(request: fastapi.Request):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    r = request.json()
    
    #Remove an image. Similar to the docker rmi command.
    #Parameters:
    #image (str) – The image to remove
    #force (bool) – Force removal of the image
    #noprune (bool) – Do not delete untagged parents
    
    image = client.images.remove(**r)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({image.id: image.tags[0]}))

@router.post("/back/docker/container/deploy")
def container_deploy(request: fastapi.Request):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    container : Container = client.containers.run(**request.json())
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], container.id)

@router.post("/back/docker/volume/create")
def volcreate(request: fastapi.Request):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    volume : Volume = client.volumes.create(**request.json())
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], volume.id)

@router.post("/back/docker/volume/remove")
def volremove(request: fastapi.Request):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    volume : Volume = client.volumes.remove(**request.json())
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], volume.id)

@router.post("/back/docker/network/create")
def netcreate(request: fastapi.Request):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    network : Network = client.networks.create(**request.json())
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], network.id)

@router.post("/back/docker/network/remove")
def netremove(request: fastapi.Request):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    network : Network = client.networks.remove(**request.json())
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], network.id)