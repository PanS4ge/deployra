from fastapi import FastAPI, APIRouter, Response
from loguru import logger
from utils import allowed
from utils import vitals, setting, diffiehellman
import json
import os
import fastapi
import docker
import typing
import statics
from nginx.config.builder import NginxConfigBuilder

from docker.models.containers import Container
from docker.models.volumes import Volume
from docker.models.networks import Network

client = docker.from_env()
router = APIRouter()

def get_docker(setting_field: str, image_name: str) -> typing.Optional[docker.models.containers.Container]:
    cache: dict = setting.read_settings() if setting.settings_exists() else {}
    if (cid := cache.get(setting_field)):
        try:
            return client.containers.get(cid)
        except docker.errors.NotFound:
            pass

    my_id = os.getenv("HOSTNAME")
    if not my_id:
        return None

    networks = client.networks.list(greedy=True)
    my_net = next(
        (n for n in networks if my_id in (n.attrs.get("Containers") or {})),
        None,
    )
    if not my_net:
        return None
    
    for c in my_net.containers:
        tags = c.image.tags or []
        if any(tag.split(":")[0] == image_name for tag in tags):
            setting.update_settings(**{setting_field: c.id})
            return c
    return None

def get_nginx_docker() -> Container | None:
    return get_docker("nginx_container_id", "nginx")

def get_certbot_docker() -> Container | None:
    return get_docker("certbot_container_id", "certbot/certbot")

@router.get("/back/nginx")
def nginx(request: fastapi.Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    ng = get_nginx_docker()
    if(ng == None):
        return Response(status_code=401)
    return diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], ng.id)

@router.post("/back/nginx/create")
async def create_nginx(request : fastapi.Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    try:
        r = await request.json()
        domain = r["domain"]
        target_container_name = r["target_container_name"]
        target_container_port = r["target_container_port"]
        ssl = r["ssl"]
        if(ssl):
            ssl_email = r["ssl_email"]
        else:
            ssl_email = ""
        ssl_done = False
        cb = get_certbot_docker()
        if(cb != None):
            cb.exec_run(("certbot certonly --webroot -w " + statics.certbot + " -d " + domain + " -d www." + domain + " --email " + ssl_email + " --agree-tos --no-eff-email").split(" "))
            cb.stop()
            ssl_done = True
        ng = get_nginx_docker()
        if(ng != None):
            nginx = NginxConfigBuilder(daemon='on')
            
            if(ssl_done):
                with nginx.add_server(listen=80, server_name=[domain, 'www.' + domain]) as server:
                    server.add_route(
                        '/.well-known/acme-challenge/',
                        root=statics.certbot
                    )
                    server.add_route(
                        '/',
                        return_='301 https://$host$request_uri'
                    )

                with nginx.add_server(
                    listen={'port': 443, 'ssl': True, 'http2': True},
                    server_name=[domain, 'www.' + domain]
                ) as server:
                    server.add_directive(
                        'ssl_certificate',
                        os.path.join(statics.letsencrypt, 'live/' + domain + '/fullchain.pem')
                    )
                    server.add_directive(
                        'ssl_certificate_key',
                        os.path.join(statics.letsencrypt, 'live/' + domain + '/privkey.pem')
                    )
                    server.add_route(
                        '/',
                        proxy_pass='http://' + target_container_name + ':' + str(target_container_port)
                    )
            else:
                with nginx.add_server(listen=80, server_name=[domain, 'www.' + domain]) as server:
                    server.add_route(
                        '/',
                        proxy_pass='http://' + target_container_name + ':' + str(target_container_port)
                    )
                    
            with open(os.path.join(statics.nginx_conf_d, domain + ".conf"), "w") as f:
                f.write(nginx)
                
            ng.exec_run(["nginx", "-s", "reload"])
            return Response(diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({"success": True, "ssl_supported": ssl_done})), status_code=200)
        return Response(status_code=500)
    except:
        return Response(status_code=400)
    
@router.post("/back/nginx/delete")
def delete_nginx(request : fastapi.Request):
    if(allowed.is_request_allowed(request) == False):
        return Response(status_code=401)
    try:
        domain = request.json()["domain"]
        ng = get_nginx_docker()
        if(ng != None):
            os.remove(os.path.join(statics.nginx_conf_d, domain + ".conf"))
            ng.exec_run(["nginx", "-s", "reload"])
            return Response(diffiehellman.encrypt_message_delivery(setting.read_settings()["shared"], json.dumps({"success": True})), status_code=200)
        return Response(status_code=500)
    except:
        return Response(status_code=400)