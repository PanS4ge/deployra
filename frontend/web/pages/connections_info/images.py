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
import random

def ensure_1_decimal(x):
    try:
        return math.floor(x * 10) / 10
    except ValueError:
        return x

@ui.page('/connections/{id}/images')
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
    @ui.refreshable
    def image_list():
        with ui.column().classes("flat border-4 border-solid border-black shadow-l rounded-[16px] items-center"):
            ui.label(settingsgen.get_language().images).classes('text-h5')
            c_id = json.loads(backend_wrapper.get_images(id))
            temp = json.loads(backend_wrapper.get_containers(id))
            used = {}
            for containers in range(len(temp.keys())):
                info = json.loads(backend_wrapper.container_info(id, temp[list(temp.keys())[containers]]))
                try:
                    if(c_id[info["Image"]] not in used.keys()):
                        used[c_id[info["Image"]]] = []
                    used[c_id[info["Image"]]].append(temp[list(temp.keys())[containers]])
                except KeyError:
                    pass
            with ui.row().classes("w-full items-center border-b border-solid border-black justify-between py-2"):
                ui.label("#").classes("w-1/13 text-center")
                ui.label(settingsgen.get_language().table_img).classes("w-1/13 text-center")
                ui.label(settingsgen.get_language().table_used).classes("w-1/13 text-center")
                ui.label(settingsgen.get_language().table_actions).classes("w-1/13 text-center")
            for images in range(len(c_id.keys())):
                with ui.row().classes("w-full items-center border-b border-solid border-black justify-between py-2"):
                    ui.label(str(images + 1) + ".").classes("w-1/13 text-center " + list(c_id.keys())[images])
                    ui.label(c_id[list(c_id.keys())[images]]).classes("w-6/13 text-center " + list(c_id.keys())[images])
                    try:
                        ui.label(", ".join(used[c_id[list(c_id.keys())[images]]])).classes("w-6/13 text-center " + list(c_id.keys())[images])
                    except KeyError:
                        ui.label("None").classes("w-6/13 text-center " + list(c_id.keys())[images])
                    with ui.column().classes("text-center flex flex-row"):
                        with ui.button("", on_click=lambda con=images: image_remove(list(c_id.keys())[con])).classes("w-[16px]"):
                            ui.html('<i class="bi bi-trash text-[24px]"></i>')
            session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
            if(session is None):
                ui.run_javascript(' window.cookies.jwt_token = ""; window.location.href = "/login";')
                return
            if(users.has_permission(session["email"], Permission.ALLOW_PULL_IMAGES)):
                with ui.row().classes("w-full items-center border-b border-solid border-black py-2"):
                    ui.button(settingsgen.get_language().pull_image, on_click=lambda: webpageutils.redirect(ui, '/connections/' + id + '/pullimage')).classes("w-full")
                            
    def image_remove(imageid):
        backend_wrapper.image_remove(id, image=imageid, force=True)
        ui.run_javascript('window.location.reload();')
    
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
                        image_list()
    if not webpageutils.is_authenticated(request):
        return webpageutils.redirect(ui, '/login')
    
    dockernavigation.create_layout(statics.__webname__ + ' - ' + settingsgen.get_storage_type().get_value(statics.db_name + "_connections", id, "name"), request, id, render)