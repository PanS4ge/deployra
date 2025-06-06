import typing
import docker
from utils import setting
import os
from docker.models.containers import Container

def get_docker(setting_field: str, image_name: str) -> typing.Optional[Container]:
    client = docker.from_env()
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