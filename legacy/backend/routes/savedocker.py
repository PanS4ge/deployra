from fastapi import FastAPI, APIRouter, Response
from loguru import logger
from utils import vitals, setting, diffiehellman, allowed
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
async def images(request: fastapi.Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    r = await request.json()
    image = client.images.pull(**r)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({image.id: image.tags[0]}))

@router.post("/back/docker/image/prune")
async def imagespurne(request: fastapi.Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    r = await request.json()
    
    #Remove an image. Similar to the docker rmi command.
    #Parameters:
    #image (str) – The image to remove
    #force (bool) – Force removal of the image
    #noprune (bool) – Do not delete untagged parents
    
    image = client.images.remove(**r)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({image.id: image.tags[0]}))

@router.post("/back/docker/image/remove")
async def imagespurne(request: fastapi.Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    r = await request.json()
    image = client.images.remove(**r)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], "yes")

@router.post("/back/docker/container/deploy")
async def container_deploy(request: fastapi.Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    r = await request.json()
    container : Container = client.containers.run(**r)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], container.id)

@router.post("/back/docker/volume/create")
async def volcreate(request: fastapi.Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    r = await request.json()
    volume : Volume = client.volumes.create(**r)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], volume.id)

@router.post("/back/docker/volume/remove")
async def volremove(request: fastapi.Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    r = await request.json()
    volume : Volume = client.volumes.prune(**r)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], volume.id)

@router.post("/back/docker/network/create")
async def netcreate(request: fastapi.Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    r = await request.json()
    if("ipam_subnet" in r and "ipam_gateway" in r):
        ipam_pool = docker.types.IPAMPool(subnet=r["ipam_subnet"], gateway=r["ipam_gateway"])
        r["ipam"] = docker.types.IPAMConfig(pool_configs=[ipam_pool])
        del r["ipam_subnet"]
        del r["ipam_gateway"]
    network : Network = client.networks.create(**r)
    return diffiehellman.encrypt_message_delivery(setting.read_sttings()["shared"], network.id)

@router.post("/back/docker/network/remove")
async def netremove(request: fastapi.Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    r = await request.json()
    network : Network = client.networks.prune(**r)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], network.id)