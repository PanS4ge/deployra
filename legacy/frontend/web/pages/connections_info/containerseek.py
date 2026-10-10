import datetime

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
        #with ui.scroll_area().classes('w-full h-[calc(100vh-200px)]'):
        if True:
            with ui.column().classes('flat'):
                with ui.row().classes('w-full flex items-center justify-between border-2 border-cyan-400 rounded-lg p-6 shadow-[0_0_15px_#06b6d4]'):
                    ui.label(settingsgen.get_language().seek_command).classes('bg-blue-300 text-white p-2 rounded-lg')
                    ui.label(" ".join(c["Args"]))
                with ui.row().classes('w-full flex items-center justify-between border-2 border-cyan-400 rounded-lg p-6 shadow-[0_0_15px_#06b6d4]'):
                    ui.label(settingsgen.get_language().seek_id).classes('bg-blue-300 text-white p-2 rounded-lg')
                    ui.label(c["Id"])
                with ui.row().classes('w-full flex items-center justify-between border-2 border-cyan-400 rounded-lg p-6 shadow-[0_0_15px_#06b6d4]'):
                    ui.label(settingsgen.get_language().seek_starttime).classes('bg-blue-300 text-white p-2 rounded-lg')
                    ui.label(str(datetime.datetime.fromisoformat(c["State"]["StartedAt"]).strftime("%Y-%m-%d %H:%M")))
                with ui.row().classes('w-full flex items-center justify-between border-2 border-cyan-400 rounded-lg p-6 shadow-[0_0_15px_#06b6d4]'):
                    ui.label(settingsgen.get_language().seek_portbindings).classes('bg-blue-300 text-white p-2 rounded-lg')
                    with ui.column():
                        if c["HostConfig"]["PortBindings"] is None:
                            ui.label(settingsgen.get_language().seek_portbindings_none).classes('text-red-500')
                        else:
                            for port, bindings in c["NetworkSettings"]["Ports"].items():
                                if bindings is not None:
                                    for binding in bindings:
                                        ui.label(f"{port} -> {binding['HostIp']}:{binding['HostPort']}")
                                else:
                                    ui.label(f"{port} -> {settingsgen.get_language().offline}")
                with ui.row().classes('w-full flex items-center justify-between border-2 border-cyan-400 rounded-lg p-6 shadow-[0_0_15px_#06b6d4]'):
                    ui.label(settingsgen.get_language().seek_mounts).classes('bg-blue-300 text-white p-2 rounded-lg')
                    with ui.column():
                        for mount in c["Mounts"]:
                            with ui.row().classes('p-2'):
                                ui.label(f"{mount['Source']} -> {mount['Destination']}")
                                if mount["Mode"]:
                                    ui.label(f"Mode: {mount['Mode']}")
                                if mount["RW"]:
                                    ui.label("RW")
                with ui.row().classes('w-full flex items-center justify-between border-2 border-cyan-400 rounded-lg p-6 shadow-[0_0_15px_#06b6d4]'):
                    ui.label(settingsgen.get_language().seek_labels).classes('bg-blue-300 text-white p-2 rounded-lg')
                    with ui.column():
                        if c["Config"]["Labels"] is None:
                            ui.label(settingsgen.get_language().seek_labels_none).classes('text-red-500')
                        else:
                            for label, value in c["Config"]["Labels"].items():
                                ui.label(f"{label}: {value}")
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
        with ui.row().classes("inline-grid items-center"):
            with ui.row():
                with ui.column():
                    if not(am_i_online()):
                        ui.label(settingsgen.get_language().no_connection).classes('text-h3')
                    else:
                        seek_container(container)
    if not webpageutils.is_authenticated(request):
        return webpageutils.redirect(ui, '/login')
    session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
    if(not users.has_permission(session["email"], Permission.ALLOW_SEEKING_CONTAINER)):
        return webpageutils.redirect(ui, '/connections')
    
    dockernavigation.create_layout(statics.__webname__ + ' - ' + settingsgen.get_storage_type().get_value(statics.db_name + "_connections", id, "name"), request, id, render)