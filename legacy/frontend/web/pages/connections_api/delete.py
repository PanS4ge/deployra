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

@ui.page('/connections/{id}/delete')
def main_page(request: Request, id: str) -> None:
    def am_i_online() -> bool:
        try:
            json.loads(backend_wrapper.connect_to_backend(id).text) # identyfikacja
            json.loads(backend_wrapper.get_vitals(id))         # czy secret jest dobry
            json.loads(backend_wrapper.get_images(id))         # czy docker jest dobry
            # jeszce tu dorzucić nginxa
            return True
        except Exception:
            return False
    
    def remove_connection():
        session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
        if session is None or not users.has_permission(session["email"], Permission.ALLOW_DELETE_DOCKERS):
            return

        try:
            backend_wrapper.free_connection(id)
            settingsgen.get_storage_type().delete_value(statics.db_name + "_connections", id)
            ui.notify(settingsgen.get_language().removed_connection, color='positive')
            webpageutils.redirect(ui, '/connections')    
        except Exception as e:
            settingsgen.get_storage_type().delete_value(statics.db_name + "_connections", id)
            ui.notify(settingsgen.get_language().removed_connection_conf, color='positive')
            webpageutils.redirect(ui, '/connections')        
    
        dialog.close()

    # -------- DIALOG (bez card) --------
    with ui.dialog() as dialog:
        with ui.row():
            with ui.column().classes("p-4 border-4 border-solid border-black bg-red-500 shadow-l rounded-[16px]"):
                first = random.randint(20, 40)
                second = random.randint(20, 40)
                eq = random.choice(['+', '-'])
                whatis = eval(f"{first}{eq}{second}")

                answ = [whatis] + [
                    whatis + offset for offset in [-random.randint(1, 15), random.randint(1, 15)]
                ]
                answ += [whatis + random.randint(-10, 10) for _ in range(4)]
                random.shuffle(answ)

                def handle_answer(ans, btn):
                    session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
                    if(session is None):
                        ui.run_javascript(' window.cookies.jwt_token = ""; window.location.href = "/login";')
                        return
                    logger.info(f"[DEBUG] clicked: {ans}")
                    if ans != whatis:
                        dialog.close()
                    else:
                        if(session is None or not users.has_permission(session["email"], Permission.ALLOW_DELETE_DOCKERS)):
                            return
                        btn.loading = True
                        btn.update()
                        remove_connection()
                        btn.loading = False
                        btn.update()

                with ui.column().classes('items-center p-4'):
                    ui.label(f"{first} {eq} {second} = ?").classes('text-lg font-semibold')

                    with ui.row().classes('gap-2'):
                        for ans in answ:
                            btn = ui.button(str(ans))
                            btn.on('click', lambda _, ans=ans, btn=btn: handle_answer(ans, btn))

                    with ui.row().classes('gap-2'):
                        ui.button(settingsgen.get_language().resign_cantcount, on_click=lambda _: dialog.close())
                        ui.button(settingsgen.get_language().resign_factory_reset, on_click=lambda _: dialog.close())
    
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

        if(users.has_permission(session["email"], Permission.ALLOW_DELETE_DOCKERS)):
            ui.button(settingsgen.get_language().remove_connection).props('outline color=negative w-480px').on('click', lambda _: dialog.open())
    if not webpageutils.is_authenticated(request):
        return webpageutils.redirect(ui, '/login')
    
    dockernavigation.create_layout(statics.__webname__ + ' - ' + settingsgen.get_storage_type().get_value(statics.db_name + "_connections", id, "name"), request, id, render)