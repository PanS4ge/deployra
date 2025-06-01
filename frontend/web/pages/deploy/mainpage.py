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
@ui.page('/deploy')
def main_page(request: Request) -> None:
    def render():
        session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
        if(session is None):
            ui.run_javascript(' window.cookies.jwt_token = ""; window.location.href = "/login";')
            return
        
        with ui.column().classes("flat border-4 border-solid border-black shadow-l rounded-[16px] items-center"):
            ui.label(settingsgen.get_language().deployments).classes('text-h5')
            with ui.row():
                with ui.column():
                    with ui.row().classes("w-full items-center border-b border-solid border-black justify-between py-2"):
                        ui.label("#").classes("w-1/13 text-center")
                        ui.label(settingsgen.get_language().table_name).classes("w-1/13 text-center")
                        ui.label(settingsgen.get_language().location).classes("w-1/13 text-center")
                        ui.label(settingsgen.get_language().address).classes("w-1/13 text-center")
                        ui.label(settingsgen.get_language().uuid).classes("w-1/13 text-center")
                        ui.label(settingsgen.get_language().deploytype).classes("w-1/13 text-center")
                        ui.label(settingsgen.get_language().table_actions).classes("w-1/13 text-center")
                    _deployments = settingsgen.get_storage_type().list_keys(statics.db_name + "_deploys")
                    deployments = [] if _deployments is None else _deployments
                    for deploynum in range(len(deployments)):
                        with ui.row("justifybetween"):
                            ui.label(str(deploynum)).classes("w-1/13 text-center " + settingsgen.get_storage_type().get_value(statics.db_name + "_deploys", deployments[deploynum], "uuid"))
                            ui.label(settingsgen.get_storage_type().get_value(statics.db_name + "_deploys", deployments[deploynum], "name")).classes("w-6/13 text-center " + settingsgen.get_storage_type().get_value(statics.db_name + "_deploys", deployments[deploynum], "uuid"))
                            ui.label(settingsgen.get_storage_type().get_value(statics.db_name + "_deploys", deployments[deploynum], "location")).classes("w-6/13 text-center " + settingsgen.get_storage_type().get_value(statics.db_name + "_deploys", deployments[deploynum], "uuid"))
                            ui.label(settingsgen.get_storage_type().get_value(statics.db_name + "_deploys", deployments[deploynum], "address")).classes("w-6/13 text-center " + settingsgen.get_storage_type().get_value(statics.db_name + "_deploys", deployments[deploynum], "uuid"))
                            ui.label(settingsgen.get_storage_type().get_value(statics.db_name + "_deploys", deployments[deploynum], "uuid")).classes("w-6/13 text-center " + settingsgen.get_storage_type().get_value(statics.db_name + "_deploys", deployments[deploynum], "uuid"))
                            ui.label(settingsgen.get_storage_type().get_value(statics.db_name + "_deploys", deployments[deploynum], "deploytype")).classes("w-6/13 text-center " + settingsgen.get_storage_type().get_value(statics.db_name + "_deploys", deployments[deploynum], "uuid"))
                            with ui.row():
                                if(users.has_permission(session, Permission.ALLOW_DEPLOYMENT)):
                                    if(len(backend_wrapper.get_all_connections()) > 0):
                                        ui.button(settingsgen.get_language().new_deploy, on_click=webpageutils.redirect(ui, '/deploy/new')).classes("w-full text-center")
                                    else:
                                        ui.button(settingsgen.get_language().add_at_least_one_connection, on_click=webpageutils.redirect(ui, '/connections')).classes("w-full text-center")
            
    if not webpageutils.is_authenticated(request):
        return webpageutils.redirect(ui, '/login')
    layout.create_layout(statics.__webname__ + ' - ' + settingsgen.get_language().deploy_page, request, render)
