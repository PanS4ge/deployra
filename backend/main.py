from fastapi import FastAPI, APIRouter, Response
import fastapi

from routes import vitals as vitalsroute
from routes import auth
from routes import readdocker
from routes import psdocker
from routes import savedocker
from routes import cnginx

from loguru import logger
import loguru
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
import subprocess

import docker

from docker.models.containers import Container
from docker.models.volumes import Volume
from docker.models.networks import Network

logger.opt(colors=True).info("Starting...")

if(len(os.environ.keys()) > 0):
    logger.info("Environment variables:")
    for key in os.environ.keys():
        logger.info(f"{key}: {os.environ[key]}")
    
import statics

logger.opt(colors=True).info("cwd: <red>" + os.getcwd() + "</red>")

logger.opt(colors=True).info("Using <yellow>python</yellow> version " + str(sys.version_info.major) + "." + str(sys.version_info.minor) + " (" + str(sys.version_info.releaselevel) + ")")
for mod in [statics, fastapi, loguru]:
    logger.opt(colors=True).info(f"Using <yellow>{mod.__name__}</yellow> version {mod.__version__}")
    
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

app.include_router(vitalsroute.router)
app.include_router(auth.router)
app.include_router(readdocker.router)
app.include_router(psdocker.router)
app.include_router(savedocker.router)
app.include_router(cnginx.router)

if not(os.path.exists(statics.nginx_conf)):
    logger.info("Creating nginx.conf")
    with open(statics.nginx_conf, "w") as f:
        f.write("""
user nginx;
worker_processes 1;
pid /var/run/nginx.pid;

error_log /etc/nginx/error.log;
access_log /etc/nginx/access.log;

events {
    worker_connections 1024;
}

http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;

    include /etc/nginx/conf.d/*.conf;

    access_log /etc/nginx/access.log;
    error_log /etc/nginx/error.log warn;

    gzip on;
    gzip_types text/plain application/xml text/css application/javascript;

    client_max_body_size 10M;
}                
""")
        
        
usingport = statics.port
isport = False

client = docker.from_env()
idm = os.getenv("HOSTNAME")
container = client.containers.get(idm)
for port, bindings in container.ports.items():
    if bindings:
        for binding in bindings:
            public_port = binding.get('HostPort')
            private_port = port.split('/')[0]
            if private_port == str(statics.port):
                usingport = int(public_port)
                isport = True
            logger.info(f"Host port {public_port} is redirected to Container port {private_port}")

if not isport:
    logger.critical("THERE IS NO PORT FROM PORT " + str(statics.port) + ", IT WILL NEVER EVER BE CONNECTED!!!")
    logger.critical("THERE IS NO PORT FROM PORT " + str(statics.port) + ", IT WILL NEVER EVER BE CONNECTED!!!")
    logger.critical("THERE IS NO PORT FROM PORT " + str(statics.port) + ", IT WILL NEVER EVER BE CONNECTED!!!")

if(__name__ == "__main__"):
    logger.success("Starting server on port " + str(usingport) + ((" NOT ON " + str(statics.port) + " YOU HAVE REDIRECTED TO " + str(usingport)) if usingport != statics.port else ""))
    if(statics.uvicorn):
        uvicorn.run(app, host=("::" if statics.ipv6 else "0.0.0.0"), port=statics.port)
    else:
        cwd = os.path.dirname(__file__)
        subprocess.run(
            ["fastapi", "run", "main.py", "--proxy-headers", "--host", ("::" if statics.ipv6 else "0.0.0.0"), "--port", str(statics.port), "--docs-url", "none", "--redoc-url", "none", "--openapi-url", "none"],
            cwd=cwd
        )