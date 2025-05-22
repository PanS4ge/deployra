from fastapi import FastAPI, APIRouter, Response
from loguru import logger
from utils import vitals, setting, diffiehellman, allowed
import json
import os
import fastapi
import statics
import socket
import random
import string
import docker

client = docker.from_env()
router = APIRouter()

@router.get("/back/docker/images")
def images(request: fastapi.Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    images = client.images.list(all=True)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({image.id: image.tags[0] if image.tags else 'untagged' for image in images}))

@router.get("/back/docker/containers")
def containers(request: fastapi.Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    containers = client.containers.list(all=True)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({c.id: c.name for c in containers}))

@router.get("/back/docker/containers/statuses")
def container_statuses(request: fastapi.Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    containers = client.containers.list(all=True)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({c.id: c.status for c in containers}))

@router.get("/back/docker/volumes")
def volumes(request: fastapi.Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    volumes = client.volumes.list()
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({v.id: v.name for v in volumes}))

@router.get("/back/docker/networks")
def networks(request: fastapi.Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    networks = client.networks.list()
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({n.id: n.name for n in networks}))