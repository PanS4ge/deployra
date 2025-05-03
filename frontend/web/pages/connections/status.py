from fastapi import Request

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

@ui.page('/connections/{id}')
def main_page(request: Request, id: str) -> None:
    @ui.refreshable
    def vitals_elements():
        def ensure_2_char_hex(hex_str: str) -> str:
            if len(hex_str) == 1:
                return "0" + hex_str
            return hex_str
        
        vitals = json.loads(backend_wrapper.get_vitals(id))
        ramperc = float(vitals["ram"])
        cpuperc = float(vitals["cpu"])
        
        with ui.row():
            with ui.column():
                with ui.row().classes("inline-flex"):
                    ui.html('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" class="bi bi-memory" viewBox="0 0 16 16"><path d="M1 3a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h4.586a1 1 0 0 0 .707-.293l.353-.353a.5.5 0 0 1 .708 0l.353.353a1 1 0 0 0 .707.293H15a1 1 0 0 0 1-1V4a1 1 0 0 0-1-1zm.5 1h3a.5.5 0 0 1 .5.5v4a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5v-4a.5.5 0 0 1 .5-.5m5 0h3a.5.5 0 0 1 .5.5v4a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5v-4a.5.5 0 0 1 .5-.5m4.5.5a.5.5 0 0 1 .5-.5h3a.5.5 0 0 1 .5.5v4a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5zM2 10v2H1v-2zm2 0v2H3v-2zm2 0v2H5v-2zm3 0v2H8v-2zm2 0v2h-1v-2zm2 0v2h-1v-2zm2 0v2h-1v-2z"/></svg>')
                    calculated = int(math.floor(255 * ramperc / 100))
                    color = "#" + ensure_2_char_hex(hex(calculated)[2:]) + ensure_2_char_hex(hex(255 - calculated)[2:]) + "00"
                    with ui.row().classes("inline-grid bg-[" + color + "] w-[" + str(ramperc) + "px] h-[21px]"):
                        pass
                    ui.label(str(ensure_1_decimal(vitals["ram"])) + "%")
                with ui.row().classes("inline-flex"):
                    ui.html('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" class="bi bi-cpu" viewBox="0 0 16 16"><path d="M5 0a.5.5 0 0 1 .5.5V2h1V.5a.5.5 0 0 1 1 0V2h1V.5a.5.5 0 0 1 1 0V2h1V.5a.5.5 0 0 1 1 0V2A2.5 2.5 0 0 1 14 4.5h1.5a.5.5 0 0 1 0 1H14v1h1.5a.5.5 0 0 1 0 1H14v1h1.5a.5.5 0 0 1 0 1H14v1h1.5a.5.5 0 0 1 0 1H14a2.5 2.5 0 0 1-2.5 2.5v1.5a.5.5 0 0 1-1 0V14h-1v1.5a.5.5 0 0 1-1 0V14h-1v1.5a.5.5 0 0 1-1 0V14h-1v1.5a.5.5 0 0 1-1 0V14A2.5 2.5 0 0 1 2 11.5H.5a.5.5 0 0 1 0-1H2v-1H.5a.5.5 0 0 1 0-1H2v-1H.5a.5.5 0 0 1 0-1H2v-1H.5a.5.5 0 0 1 0-1H2A2.5 2.5 0 0 1 4.5 2V.5A.5.5 0 0 1 5 0m-.5 3A1.5 1.5 0 0 0 3 4.5v7A1.5 1.5 0 0 0 4.5 13h7a1.5 1.5 0 0 0 1.5-1.5v-7A1.5 1.5 0 0 0 11.5 3zM5 6.5A1.5 1.5 0 0 1 6.5 5h3A1.5 1.5 0 0 1 11 6.5v3A1.5 1.5 0 0 1 9.5 11h-3A1.5 1.5 0 0 1 5 9.5zM6.5 6a.5.5 0 0 0-.5.5v3a.5.5 0 0 0 .5.5h3a.5.5 0 0 0 .5-.5v-3a.5.5 0 0 0-.5-.5z"/></svg>')
                    calculated = int(math.floor(255 * cpuperc / 100))
                    color = "#" + ensure_2_char_hex(hex(calculated)[2:]) + ensure_2_char_hex(hex(255 - calculated)[2:]) + "00"
                    with ui.row().classes("inline-grid bg-[" + color + "] w-[" + str(cpuperc) + "px] h-[21px]"):
                        pass
                    ui.label(str(ensure_1_decimal(vitals["cpu"])) + "%")
            with ui.dropdown_button(settingsgen.get_language().cpucores_name):
                with ui.column().classes("grid p-3"):
                    bk : dict = json.loads(backend_wrapper.get_all_cpu_cores(id))
                    for core in bk.items():
                        with ui.row().classes("inline-flex"):
                            ui.label(core[0])
                            ui.html('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" class="bi bi-cpu" viewBox="0 0 16 16"><path d="M5 0a.5.5 0 0 1 .5.5V2h1V.5a.5.5 0 0 1 1 0V2h1V.5a.5.5 0 0 1 1 0V2h1V.5a.5.5 0 0 1 1 0V2A2.5 2.5 0 0 1 14 4.5h1.5a.5.5 0 0 1 0 1H14v1h1.5a.5.5 0 0 1 0 1H14v1h1.5a.5.5 0 0 1 0 1H14v1h1.5a.5.5 0 0 1 0 1H14a2.5 2.5 0 0 1-2.5 2.5v1.5a.5.5 0 0 1-1 0V14h-1v1.5a.5.5 0 0 1-1 0V14h-1v1.5a.5.5 0 0 1-1 0V14h-1v1.5a.5.5 0 0 1-1 0V14A2.5 2.5 0 0 1 2 11.5H.5a.5.5 0 0 1 0-1H2v-1H.5a.5.5 0 0 1 0-1H2v-1H.5a.5.5 0 0 1 0-1H2v-1H.5a.5.5 0 0 1 0-1H2A2.5 2.5 0 0 1 4.5 2V.5A.5.5 0 0 1 5 0m-.5 3A1.5 1.5 0 0 0 3 4.5v7A1.5 1.5 0 0 0 4.5 13h7a1.5 1.5 0 0 0 1.5-1.5v-7A1.5 1.5 0 0 0 11.5 3zM5 6.5A1.5 1.5 0 0 1 6.5 5h3A1.5 1.5 0 0 1 11 6.5v3A1.5 1.5 0 0 1 9.5 11h-3A1.5 1.5 0 0 1 5 9.5zM6.5 6a.5.5 0 0 0-.5.5v3a.5.5 0 0 0 .5.5h3a.5.5 0 0 0 .5-.5v-3a.5.5 0 0 0-.5-.5z"/></svg>')
                            calculated = int(math.floor(255 * core[1] / 100))
                            color = "#" + ensure_2_char_hex(hex(calculated)[2:]) + ensure_2_char_hex(hex(255 - calculated)[2:]) + "00"
                            with ui.row().classes("inline-grid bg-[" + color + "] w-[" + str(cpuperc) + "px] h-[21px]"):
                                pass
                            ui.label(str(ensure_1_decimal(core[1])) + "%")
    
    def remove_connection():
        session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
        if session is None or not users.has_permission(session["email"], Permission.ALLOW_DELETE_DOCKERS):
            return

        backend_wrapper.free_connection(id)
        settingsgen.get_storage_type().delete_value(statics.db_name + "_connections", id)
        ui.notify(settingsgen.get_language().removed_connection, color='positive')
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
                    logger.info(f"[DEBUG] clicked: {ans}")
                    if ans != whatis:
                        dialog.close()
                    else:
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
        with ui.row().classes("inline-grid absolute-center items-center"):
            with ui.column():
                vitals_elements()
            with ui.column():
                    ui.button(settingsgen.get_language().remove_connection)\
                        .props('outline color=negative')\
                        .on('click', lambda _: dialog.open())
    if not webpageutils.is_authenticated(request):
        return webpageutils.redirect(ui, '/login')
    
    layout.create_layout(statics.__webname__ + ' - ' + settingsgen.get_language().connections_page, request, render)
    ui.timer(statics.timer_interval, vitals_elements.refresh, immediate=True)