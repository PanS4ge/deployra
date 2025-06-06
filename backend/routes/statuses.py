from fastapi import FastAPI, APIRouter, Response
from utils import vitals, setting, diffiehellman, allowed
import docker
from fastapi import Request
from utils import ftp as ftpserver
from utils import dockersearcher
import json
import os
import fastapi
import random

client = docker.from_env()
router = APIRouter()

@router.post("/back/status/certbot")
async def certbot_on(request: Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({"status": dockersearcher.get_certbot_docker().status}))

@router.post("/back/status/nginx")
async def nginx_on(request: Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({"status": dockersearcher.get_nginx_docker().status}))