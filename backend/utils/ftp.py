import docker
import docker.types
import statics
from utils.dockersearcher import get_nginx_docker, get_certbot_docker
import os
from docker.models.containers import Container

def create_ftp(client : docker.DockerClient, username : str, password : str, domain : str, port : int, ssl_enabled : bool, ssl_email : str, start_pasv_port : int, end_pasv_port : int) -> Container:
    ports = {
        f"{port}/tcp": port,   
    }
    for i in range(start_pasv_port, end_pasv_port + 1):
        ports[f"{i}/tcp"] = i
    if(ssl_enabled):
        cb = get_certbot_docker()
        if(cb != None):
            cb.exec_run(("certbot certonly --standalone --preferred-challenges http -n --agree-tos --email " + ssl_email + " -d " + domain).split(" "))
            cb.stop()
    ftp = client.containers.run(
        image=statics.ftp_image,
        name='deployra_ftp_post',
        detach=True,
        ports=ports,
        environment={
            'USERS': f'{username}|{password}|/data|10000',
            'ADDRESS': domain,
            'TLS_CERT': os.path.join(statics.letsencrypt, 'live', domain, 'cert.pem'),
            'TLS_KEY': os.path.join(statics.letsencrypt, 'live', domain, 'privkey.pem'),
        },
        volumes={
            'apps_data': {
                'bind': '/data',
                'mode': 'rw',
            },
            'certbot_conf': {
                'bind': '/etc/letsencrypt',
                'mode': 'ro',
            }
        },
        restart_policy={"Name": "unless-stopped"}
    )
    return ftp