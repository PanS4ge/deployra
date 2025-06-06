from fastapi import FastAPI, APIRouter, Response
from utils import vitals, setting, diffiehellman, allowed
import docker
from fastapi import Request
import json
import random

client = docker.from_env()
router = APIRouter()

@router.post("/back/play/rps")
async def rps(request: Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({"chosen": random.choice(["rock", "paper", "scissors"])}))