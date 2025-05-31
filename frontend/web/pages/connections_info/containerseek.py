from fastapi import Request

from web.pages import dockernavigation
from utils.encryption import jwthandle
import statics
import json

from utils import backend_wrapper
from utils import settingsgen
from utils.managers import users
from utils.permissions import Permission
import web.layout as layout
from nicegui import ui
from nicegui import APIRouter
from utils import backend_wrapper
from utils import webpageutils
from utils.managers import notifications
import math

from loguru import logger
from utils.caching import ttl_cache, local_cache
import random

def ensure_1_decimal(x):
    try:
        return math.floor(x * 10) / 10
    except ValueError:
        return x

@ui.page('/connections/{id}/seek/{container}')
def main_page(request: Request, id: str, container: str) -> None:
    def am_i_online() -> bool:
        try:
            backend_wrapper.connect_to_backend(id) # identyfikacja
            backend_wrapper.get_vitals(id)         # czy secret jest dobry
            backend_wrapper.get_volumes(id)         # czy docker jest dobry
            # jeszce tu dorzucić nginxa
            return True
        except Exception:
            return False
        
    def seek_container(container: str):
        c = json.loads(backend_wrapper.container_info(id, container))
        ui.textarea(label=settingsgen.get_language().dev_container_info, value=json.dumps(c, indent=4)).classes('w-[800px]')
    
    def render():
        session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
        if(session is None):
            ui.run_javascript(' window.cookies.jwt_token = ""; window.location.href = "/login";')
            return
        if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
            ui.run_javascript('window.location.href = "/connections";')
            return
        if(not settingsgen.get_storage_type().get_value(statics.db_name + "_connections", id, "access_key")):
            # o czym my wogóle tu gadamy?
            return
        with ui.row().classes("inline-grid absolute-center items-center"):
            with ui.row():
                with ui.column():
                    if not(am_i_online()):
                        ui.label(settingsgen.get_language().no_connection).classes('text-h3')
                    else:
                        seek_container(container)
    if not webpageutils.is_authenticated(request):
        return webpageutils.redirect(ui, '/login')
    
    dockernavigation.create_layout(statics.__webname__ + ' - ' + settingsgen.get_storage_type().get_value(statics.db_name + "_connections", id, "name"), request, id, render)