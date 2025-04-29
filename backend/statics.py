import hashlib
import string
import random
import os

__name__ = "Deployra"
__version__ = "0.0.1"
secret = hashlib.sha256(("".join([random.choice(string.hexdigits) for _ in range(24)])).encode()).hexdigest()

if(not os.getcwd().endswith("backend")): os.chdir("backend")

port = 2750

uvicorn = False
access_key_length = 48