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

@ui.page('/connections/{id}/network/new')
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
        
    def create_network(name, driver, options, check_duplicate, internal, labels, enable_ipv6, attachable, scope, ingress, ipam_subnet, ipam_gateway, id):
        try:
            c = backend_wrapper.network_create(id, name=name, driver=(driver if driver else "local"), options=json.loads(options), check_duplicate=bool(check_duplicate), internal=bool(internal), labels=json.loads(labels), enable_ipv6=bool(enable_ipv6), attachable=bool(attachable), scope=scope, ingress=bool(ingress), ipam_subnet=ipam_subnet, ipam_gateway=ipam_gateway)
        except:
            traceback.print_exc()
            ui.notify(settingsgen.get_language().net_makesure, color='negative')
            return None
        try:
            notifications.add_notification(settingsgen.get_language().created_volume_success.replace("{ID}", c).replace("{DOCKER}", settingsgen.get_storage_type().get_value(statics.db_name + "_connections", id, "name")))
            ui.notify(settingsgen.get_language().created_volume_success.replace("{ID}", c).replace("{DOCKER}", settingsgen.get_storage_type().get_value(statics.db_name + "_connections", id, "name")))
        except:
            traceback.print_exc()
            ui.notify(settingsgen.get_language().created_volume_error, color='negative')
            return None
        
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
                        name = ui.input(label=settingsgen.get_language().net_name, placeholder=settingsgen.get_language().net_name_ph).classes('w-3/4')
                        driver = ui.input(label=settingsgen.get_language().net_driver, placeholder=settingsgen.get_language().net_driver_ph).classes('w-3/4')
                        options = ui.textarea(label=settingsgen.get_language().net_options, placeholder=settingsgen.get_language().net_options_ph).classes('w-3/4')
                        check_duplicate = ui.checkbox(label=settingsgen.get_language().net_check_duplicate).classes('w-3/4')
                        internal = ui.checkbox(label=settingsgen.get_language().net_internal).classes('w-3/4')
                        labels = ui.textarea(label=settingsgen.get_language().net_labels, placeholder=settingsgen.get_language().net_labels_ph).classes('w-3/4')
                        enable_ipv6 = ui.checkbox(label=settingsgen.get_language().net_enable_ipv6).classes('w-3/4')
                        attachable = ui.checkbox(label=settingsgen.get_language().net_attachable).classes('w-3/4')
                        scope = ui.select(options=["local", "global", "swarm"], label=settingsgen.get_language().net_scope, placeholder=settingsgen.get_language().net_scope_ph).classes('w-3/4')
                        ingress = ui.checkbox(label=settingsgen.get_language().net_ingress).classes('w-3/4')
                        ipam_subnet = ui.input(label=settingsgen.get_language().net_ipam_subnet, placeholder=settingsgen.get_language().net_ipam_subnet_ph).classes('w-3/4')
                        ipam_gateway = ui.input(label=settingsgen.get_language().net_ipam_gateway, placeholder=settingsgen.get_language().net_ipam_gateway_ph).classes('w-3/4')
                        ui.button(text=settingsgen.get_language().net_create, on_click=lambda name=name, driver=driver, options=options, check_duplicate=check_duplicate, internal=internal, labels=labels, enable_ipv6=enable_ipv6, attachable=attachable, scope=scope, ingress=ingress, ipam_subnet=ipam_subnet, ipam_gateway=ipam_gateway: create_network(name, driver, options, check_duplicate, internal, labels, enable_ipv6, attachable, scope, ingress, ipam_subnet, ipam_gateway, id)).classes('w-3/4')
                        ui.button(text=settingsgen.get_language().goback, on_click=lambda: webpageutils.redirect(ui, "/connections/" + id + "/volumes")).classes('w-3/4')
    if not webpageutils.is_authenticated(request):
        return webpageutils.redirect(ui, '/login')
    session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
    if(not users.has_permission(session["email"], Permission.ALLOW_CREATING_VOLUMES)):
        return webpageutils.redirect(ui, '/connections')
    
    dockernavigation.create_layout(statics.__webname__ + ' - ' + settingsgen.get_storage_type().get_value(statics.db_name + "_connections", id, "name"), request, id, render)