import hashlib
import string
import random
import os

__name__ = "DeployraAPI"
__version__ = "0.0.2"
secret = hashlib.sha256(("".join([random.choice(string.hexdigits) for _ in range(24)])).encode()).hexdigest()

port = 2750

try:
    access_key_length = int(os.environ.get("access_key_length", 48))
except ValueError:
    access_key_length = 48
    print("Invalid value for access_key_length, using default: 48")

try:
    cpu_usage_interval = float(os.environ.get("cpu_usage_interval", 0.1))
except ValueError:
    cpu_usage_interval = 0.1
    print("Invalid value for cpu_usage_interval, using default: 0.1")


apps_folder   = "/apps"
letsencrypt   = "/etc/letsencrypt"
certbot       = "/var/www/certbot"
nginx_conf    = "/etc/nginx/nginx.conf"
nginx_conf_d  = "/etc/nginx/conf.d"

if(not os.path.exists(apps_folder)): os.mkdir(apps_folder)
if(not os.path.exists(letsencrypt)): os.mkdir(letsencrypt)
if(not os.path.exists(certbot)): os.mkdir(certbot)
if(not os.path.exists(os.path.dirname(nginx_conf))): os.mkdir(os.path.dirname(nginx_conf_d))
if(not os.path.exists(nginx_conf_d)): os.mkdir(nginx_conf_d)

ipv6 = os.environ.get("ipv6", "false").lower() == "true"
uvicorn = os.environ.get("uvicorn", "true").lower() == "true"