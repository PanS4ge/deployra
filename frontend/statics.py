import hashlib
from utils.languages import languagefile
import string
import random
import os

__name__ = "Deployra"
__webname__ = "Deployra Web"
__version__ = "0.0.2"
secret = hashlib.sha256(("".join([random.choice(string.hexdigits) for _ in range(24)])).encode()).hexdigest()

db_name = "deployra"

randomport = True
port = 8080
default_backend_port = 2750

private_key = os.path.join(os.getcwd(), "certs", "id_rsa")
public_key = os.path.join(os.getcwd(), "certs", "id_rsa.pub")
bundled = not os.getcwd().endswith("frontend")
example_domain = "deployra.local"
debug = True

uvicorn = True

ipv6 = False
reload = False
timer_interval = 1000

max_notifications = 30
gravatar_default_default = "https://cdn.pfps.gg/pfps/9262-cat.png"

default_language = "Polish"
possible_languages = [
    "Polish"
]
storage_options = [
    "JSON",
    "Encrypted JSON",
    "MySQL"
]
dns_options = [
    "None",
    "Cloudflare Token"
]