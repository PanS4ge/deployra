from fastapi import FastAPI, Response
import fastapi
from loguru import logger
import loguru
import statics
import os
import sys
from utils import vitals
from utils import setting
from utils import diffiehellman
import random
import string
import json
import socket
import uvicorn
import base64

logger.opt(colors=True).info("cwd: <red>" + os.getcwd() + "</red>")

logger.opt(colors=True).info("Using <yellow>python</yellow> version " + str(sys.version_info.major) + "." + str(sys.version_info.minor) + " (" + str(sys.version_info.releaselevel) + ")")
for mod in [statics, fastapi, loguru]:
    logger.opt(colors=True).info(f"Using <yellow>{mod.__name__}</yellow> version {mod.__version__}")
    
app = FastAPI()

@app.get("/back/vitals/ramusage")
def ram(request: fastapi.Request):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({"ram": vitals.get_ram_usage_percentage()}))

@app.get("/back/vitals/amount")
def maxvitals(request: fastapi.Request):
    if(not setting.settings_exists()): return Response(status_code=412)
    if not("authorization" in request.headers.keys()):
        return Response(status_code=401)
    if(diffiehellman.decrypt_message_delivery(setting.read_settings()["shared"], request.headers.get("authorization")) != setting.read_settings()["access_key"]):
        return Response(status_code=404)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({"ram": vitals.get_ram_amount(), "cpu": vitals.get_cpu_core_count(), "rawram": vitals.get_raw_ram_amount()}))

@app.get("/back/identify")
def identify(request: fastapi.Request):
    return {"id": statics.__name__ + " " + statics.__version__, "host": socket.gethostname()}

@app.post("/back/exchange")
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

if(__name__ == "__main__"):
    logger.success("Starting server, QUICKLY ADD TO " + statics.__name__.upper())
    uvicorn.run(app, host="0.0.0.0", port=statics.port)