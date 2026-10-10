from fastapi import FastAPI, APIRouter, Response
from utils import vitals, setting, diffiehellman, allowed
import docker
from fastapi import Request
from utils import ftp as ftpserver
import json
import os
import fastapi

client = docker.from_env()
router = APIRouter()

@router.post("/back/createftp")
async def ftp(request: Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    
    jsong = await request.json()
    jsong["client"] = client
    ftp = ftpserver.create_ftp(**jsong)
    
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({"id": ftp.id}))