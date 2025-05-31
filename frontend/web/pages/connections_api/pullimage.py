from fastapi import Request

from web.pages import dockernavigation
from utils.encryption import jwthandle
import statics
import json
import traceback

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
import random

def ensure_1_decimal(x):
    try:
        return math.floor(x * 10) / 10
    except ValueError:
        return x

@ui.page('/connections/{id}/pullimage')
def main_page(request: Request, id: str) -> None:
    def am_i_online() -> bool:
        try:
            backend_wrapper.connect_to_backend(id) # identyfikacja
            backend_wrapper.get_vitals(id)         # czy secret jest dobry
            backend_wrapper.get_images(id)         # czy docker jest dobry
            # jeszce tu dorzucić nginxa
            return True
        except Exception:
            return False
        
    def pull_image(ih, it, id):
        c = backend_wrapper.pull_image(id, repository=ih.value, tag=it.value)
        try:
            ret : dict = json.loads(c)
            print(ret)
            notifications.add_notification(settingsgen.get_language().image_pull_success.replace("{IMAGE}", list(ret.values())[0]).replace("{DOCKER}", settingsgen.get_storage_type().get_value(statics.db_name + "_connections", id, "name")))
            ui.notify(settingsgen.get_language().image_pull_success.replace("{IMAGE}", list(ret.values())[0]).replace("{DOCKER}", settingsgen.get_storage_type().get_value(statics.db_name + "_connections", id, "name")))
        except:
            traceback.print_exc()
            ui.notify(settingsgen.get_language().image_pull_error, color='negative')
            return None
        ui.label(ret).classes('text-h3')
        
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
        with ui.row().classes("inline-grid absolute-center items-center flat"):
            with ui.row():
                with ui.column():
                    if not(am_i_online()):
                        ui.label(settingsgen.get_language().no_connection).classes('text-h3')
                    else:
                        it = ui.input(label=settingsgen.get_language().image_tag, placeholder=settingsgen.get_language().image_tag_ph).classes('w-3/4')
                        ih = ui.input(label=settingsgen.get_language().image_host, placeholder=settingsgen.get_language().image_host_ph).classes('w-3/4')
                        ui.button(text=settingsgen.get_language().pull_image, on_click=lambda it=it, ih=ih, id=id: pull_image(it, ih, id)).classes('w-3/4')
                        ui.button(text=settingsgen.get_language().goback, on_click=lambda: webpageutils.redirect(ui, "/connections/" + id + "/images")).classes('w-3/4')
    if not webpageutils.is_authenticated(request):
        return webpageutils.redirect(ui, '/login')
    session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
    if(not users.has_permission(session["email"], Permission.ALLOW_PULL_IMAGES)):
        return webpageutils.redirect(ui, '/connections')
    
    dockernavigation.create_layout(statics.__webname__ + ' - ' + settingsgen.get_storage_type().get_value(statics.db_name + "_connections", id, "name"), request, id, render)