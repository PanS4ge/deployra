from multiprocessing import freeze_support
import statics
import importlib

from loguru import logger
import glob

from starlette.middleware.sessions import SessionMiddleware

from nicegui import app, ui
from nicegui import native
import os

from utils import certgen
from web.setupredirect import SetupRedirectMiddleware

logger.opt(colors=True).info("Starting webpage")

app.add_middleware(SetupRedirectMiddleware)
app.add_middleware(SessionMiddleware, secret_key=statics.secret)

for f in glob.glob("web/pages/*/*.py", recursive=True):
    package = f.replace("/", ".").replace("\\", ".").replace(".py", "")
    module = importlib.import_module(package)
    logger.success("Loaded " + package)

app.add_static_files('/assets/', 'assets')

logger.info("Generating certificates")
if not(os.path.exists(statics.public_key) and os.path.exists(statics.private_key)):
    try:
        os.mkdir(os.path.join(os.getcwd(), "certs"))
    except FileExistsError:
        pass
    certgen.generate_keys(statics.private_key, statics.public_key)
else:
    logger.info("Certificates already exist")

deployra_port = None
if(statics.randomport):
    deployra_port = native.find_open_port()
else:
    deployra_port = statics.port

if(statics.openwebbrowser and __name__ != "webpage"):
    import webbrowser
    webbrowser.open("http://127.0.0.1:" + str(deployra_port))
    
ui.run(host="0.0.0.0", port=deployra_port)
