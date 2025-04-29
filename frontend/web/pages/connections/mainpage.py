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

from utils import webpageutils
@ui.page('/connections')
def main_page(request: Request) -> None:
    def render():
        session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
        if(session is None):
            ui.run_javascript(' window.cookies.jwt_token = ""; window.location.href = "/login";')
            return
        with ui.column():
            if(settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
                keys = settingsgen.get_storage_type().list_keys(statics.db_name + "_connections")
                if(keys is not None):
                    for l in settingsgen.get_storage_type().list_keys(statics.db_name + "_connections"):
                        online = backend_wrapper.connect_to_backend(l) != False
                        active = "bg-green-500" if online else "bg-red-500"
                        with ui.row().classes('rounded-2xl border-4 border-solid border-black ' + active + ' shadow-l ps-4'):
                            with ui.row().classes('rounded-2xl bg-white shadow-l p-4'):
                                with ui.column().classes("p-0"):
                                    ui.label(l).classes('text-h6')
                                    if online:
                                        ui.label(backend_wrapper.connect_to_backend(l).json()["id"])
                                        ui.label(settingsgen.get_language().ramusage + ": " + str(json.loads(backend_wrapper.get_vitals(l))["ram"]) + "%")
                                    else:
                                        ui.label(settingsgen.get_language().offline)
                                    with ui.row().classes("inline-flex"):
                                        ui.html('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" class="bi bi-cpu" viewBox="0 0 16 16"><path d="M5 0a.5.5 0 0 1 .5.5V2h1V.5a.5.5 0 0 1 1 0V2h1V.5a.5.5 0 0 1 1 0V2h1V.5a.5.5 0 0 1 1 0V2A2.5 2.5 0 0 1 14 4.5h1.5a.5.5 0 0 1 0 1H14v1h1.5a.5.5 0 0 1 0 1H14v1h1.5a.5.5 0 0 1 0 1H14v1h1.5a.5.5 0 0 1 0 1H14a2.5 2.5 0 0 1-2.5 2.5v1.5a.5.5 0 0 1-1 0V14h-1v1.5a.5.5 0 0 1-1 0V14h-1v1.5a.5.5 0 0 1-1 0V14h-1v1.5a.5.5 0 0 1-1 0V14A2.5 2.5 0 0 1 2 11.5H.5a.5.5 0 0 1 0-1H2v-1H.5a.5.5 0 0 1 0-1H2v-1H.5a.5.5 0 0 1 0-1H2v-1H.5a.5.5 0 0 1 0-1H2A2.5 2.5 0 0 1 4.5 2V.5A.5.5 0 0 1 5 0m-.5 3A1.5 1.5 0 0 0 3 4.5v7A1.5 1.5 0 0 0 4.5 13h7a1.5 1.5 0 0 0 1.5-1.5v-7A1.5 1.5 0 0 0 11.5 3zM5 6.5A1.5 1.5 0 0 1 6.5 5h3A1.5 1.5 0 0 1 11 6.5v3A1.5 1.5 0 0 1 9.5 11h-3A1.5 1.5 0 0 1 5 9.5zM6.5 6a.5.5 0 0 0-.5.5v3a.5.5 0 0 0 .5.5h3a.5.5 0 0 0 .5-.5v-3a.5.5 0 0 0-.5-.5z"/></svg> ' + str(backend_wrapper.get_available_vitals(l)["cpu"]) + " " + settingsgen.get_language().cpucores + "<br>")
                                        ui.html('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" class="bi bi-memory" viewBox="0 0 16 16"><path d="M1 3a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h4.586a1 1 0 0 0 .707-.293l.353-.353a.5.5 0 0 1 .708 0l.353.353a1 1 0 0 0 .707.293H15a1 1 0 0 0 1-1V4a1 1 0 0 0-1-1zm.5 1h3a.5.5 0 0 1 .5.5v4a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5v-4a.5.5 0 0 1 .5-.5m5 0h3a.5.5 0 0 1 .5.5v4a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5v-4a.5.5 0 0 1 .5-.5m4.5.5a.5.5 0 0 1 .5-.5h3a.5.5 0 0 1 .5.5v4a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5zM2 10v2H1v-2zm2 0v2H3v-2zm2 0v2H5v-2zm3 0v2H8v-2zm2 0v2h-1v-2zm2 0v2h-1v-2zm2 0v2h-1v-2z"/></svg> ' + str(backend_wrapper.get_available_vitals(l)["ram"]))
                                    if(statics.debug):
                                        backend_wrapper.get_available_vitals.cache_clear()
                                with ui.row().classes('items-center gap-4 inset-y-0 right-0 pr-2'):
                                    with ui.button().classes("p-1 rounded flex items-center justify-center ml-auto").on("click", lambda _, l=l: webpageutils.redirect(ui, '/connections/' + l)):
                                        ui.html('<i class="bi bi-search text-[24px]"></i>')
                                    with ui.button().classes("p-1 rounded flex items-center justify-center ml-auto").on("click", lambda _, l=l: webpageutils.redirect(ui, '/connections/' + l + '/delete')):
                                        ui.html('<i class="bi bi-trash text-[24px]"></i>')
            
            if(users.has_permission(session["email"], Permission.ALLOW_ADD_DOCKERS)):
                with ui.row().classes('rounded-2xl border-4 border-solid border-black shadow-l grid items-center justify-center bg-white w-[550.64px] h-[183px]'):
                    with ui.button().classes("p-1 rounded flat flex items-center justify-center ml-auto rounded-full").on("click", lambda _: webpageutils.redirect(ui, '/connections/create')):
                        ui.html('<i class="bi bi-plus-circle text-[24px]"></i> ' + settingsgen.get_language().add_docker)
    if not webpageutils.is_authenticated(request):
        return webpageutils.redirect(ui, '/login')
    layout.create_layout(statics.__webname__ + ' - ' + settingsgen.get_language().connections_page, request, render)
