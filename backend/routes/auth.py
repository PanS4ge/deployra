from fastapi import FastAPI, APIRouter, Request, Response
from loguru import logger
from utils import vitals, setting, diffiehellman, allowed
import json
import os
import fastapi
import statics
import socket
import random
import string

router = APIRouter()

@router.get("/back/identify")
def identify(request: fastapi.Request):
    return {"id": statics.__name__ + " " + statics.__version__, "host": socket.gethostname()}

@router.post("/back/exchange")
async def exchange(request: fastapi.Request):
    if(setting.settings_exists()):
        return Response(status_code=404)
    if(request.method != "POST"):
        return Response(status_code=404)
    json = await request.json()
    if("pub" in json.keys() and "prime" in json.keys() and "gen" in json.keys()):
        priv_key = diffiehellman.generate_private_key(random.randint(json["prime"] * 3, json["prime"] * 10))
        shared_key = diffiehellman.generate_shared_secret(json["pub"], priv_key, json["prime"])
        pub_key = diffiehellman.generate_public_key(json["gen"], json["prime"], priv_key)
        access_key = "".join([random.choice(string.ascii_letters + string.digits) for _ in range(statics.access_key_length)])
        setting.update_settings(shared=shared_key, priv=priv_key, pub=pub_key, access_key=access_key)
        return {"pub": pub_key, "access": access_key}
    return Response(status_code=404)

@router.get("/back/remove")
def remove(request: fastapi.Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    logger.info("Removing settings.json")
    os.remove(os.path.join(os.getcwd(), "settings.json"))
    if(os.path.exists(os.path.join(os.getcwd(), "settings.json"))):
        return Response(status_code=500)
    return Response(status_code=200)

@router.post("/back/saveip")
async def saveip(request: Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    
    body = await request.json()
    setting.update_settings(ip=body["ip"])
    
    return Response(status_code=200)