import hashlib
from utils.languages import languagefile
import string
import random
import os

__name__ = "Deployra"
__webname__ = "Deployra Web"
__version__ = "0.0.1"
secret = hashlib.sha256(("".join([random.choice(string.hexdigits) for _ in range(24)])).encode()).hexdigest()

if(not os.getcwd().endswith("frontend")): os.chdir("frontend")

db_name = "deployra"

port = 8080

private_key = os.path.join(os.getcwd(), "certs", "id_rsa")
public_key = os.path.join(os.getcwd(), "certs", "id_rsa.pub")
bundled = not os.getcwd().endswith("frontend")
example_domain = "deployra.local"
debug = True

uvicorn = True

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