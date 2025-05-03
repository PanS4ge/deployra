from multiprocessing import freeze_support
import statics
import importlib

from loguru import logger
import glob

from starlette.middleware.sessions import SessionMiddleware

from nicegui import app, ui
import os

from utils import certgen
from web.setupredirect import SetupRedirectMiddleware

logger.opt(colors=True).info("Starting webpage")

app.add_middleware(SetupRedirectMiddleware)
app.add_middleware(SessionMiddleware, secret_key=statics.secret)

logger.info("Generating certificates")
if not(os.path.exists(statics.public_key) and os.path.exists(statics.private_key)):
    try:
        os.mkdir(os.path.join(os.getcwd(), "certs"))
    except FileExistsError:
        pass
    certgen.generate_keys(statics.private_key, statics.public_key)

for f in glob.glob("web/pages/*/*.py", recursive=True):
    package = f.replace("/", ".").replace("\\", ".").replace(".py", "")
    module = importlib.import_module(package)
    logger.success("Loaded " + package)

app.add_static_files('/assets/', 'assets')

if(statics.randomport):
    ui.run(host="0.0.0.0")
else:
    ui.run(host="0.0.0.0", port=statics.port)
