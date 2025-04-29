from fastapi import Request

from utils.encryption import jwthandle
import statics

from utils import settingsgen
import web.layout as layout
from nicegui import ui
from nicegui import APIRouter

from utils import webpageutils
@ui.page('/')
def main_page(request: Request) -> None:
    def render():
        session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
        if(session is None):
            ui.run_javascript(' window.cookies.jwt_token = ""; window.location.href = "/login";')
            return
        with ui.column().classes('absolute-center items-center'):
            ui.label(f'Hello {session["email"]}!').classes('text-2xl')
            ui.button('', on_click=lambda: webpageutils.redirect(ui, '/logout')).props('outline round icon=logout')
    if not webpageutils.is_authenticated(request):
        return webpageutils.redirect(ui, '/login')
    layout.create_layout(statics.__webname__ + ' - ' + settingsgen.get_language().mainpage_page, request, render)
