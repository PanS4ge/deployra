from fastapi import Request

from utils.encryption import jwthandle
import statics

from utils import backend_wrapper
from utils import settingsgen
import web.layout as layout
from nicegui import ui
from nicegui import APIRouter
import traceback
from utils import webpageutils
@ui.page('/connections/create')
def main_page(request: Request) -> None:
    def check_connection(url, b, hostname):
        b.text = settingsgen.get_language().working
        b.disable()
        b.update()
        out = backend_wrapper.check_backend(url.value)
        if out is not None:
            ui.notify(settingsgen.get_language().found + " " + out.json()["id"], color='positive')
            hostname.text = out.json()["host"]
            hostname.update()
        else:
            ui.notify(settingsgen.get_language().notfoundbackend, color='negative')
        b.text = settingsgen.get_language().c_check
        b.enable()
        b.update()
    
    def render():
        session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
        if(session is None):
            ui.run_javascript(' window.cookies.jwt_token = ""; window.location.href = "/login";')
            return
        with ui.column().classes("absolute-center items-center"):
            with ui.row().classes("items-center"):
                ui.label(settingsgen.get_language().createnew_page).classes('text-h5')
            with ui.card():
                name = ui.input(label=settingsgen.get_language().c_name, placeholder=settingsgen.get_language().c_name)
                url = ui.input(label=settingsgen.get_language().c_url, placeholder=settingsgen.get_language().c_url)
                b = ui.button(text=settingsgen.get_language().c_check)
            with ui.row().classes("items-center"):
                ui.label(settingsgen.get_language().hostname)
                hostname = ui.label()
            b.on_click(lambda url=url, b=b, hostname=hostname: check_connection(url, b, hostname))
            def save_connection(c : ui.button):
                c.text = settingsgen.get_language().working
                c.disable()
                c.update()
                try:
                    temp = backend_wrapper.exchange_dh(url.value)
                    shared = temp[0]
                    access = temp[1]
                    if(statics.debug):
                        backend_wrapper.exchange_dh.cache_clear()
                    if(shared is None):
                        ui.notify(settingsgen.get_language().notfoundbackend, color='negative')
                        c.text = settingsgen.get_language().save
                        c.enable()
                        c.update()
                    back = backend_wrapper.save_backend(url.value, name.value, shared, access)
                    if(back):
                        ui.notify(settingsgen.get_language().saved, color='positive')
                        ui.run_javascript(' setTimeout(() => { window.location.href = "/connections"; }, 3000);')
                    else:
                        ui.notify(settingsgen.get_language().something_went_wrong, color='negative')
                        c.text = settingsgen.get_language().save
                        c.enable()
                        c.update()
                except Exception as e:
                    traceback.print_exc()
                    ui.notify(settingsgen.get_language().notfoundbackend, color='negative')
                    c.text = settingsgen.get_language().save
                    c.enable()
                    c.update()
            with ui.row().classes("items-center"):
                c = ui.button(settingsgen.get_language().save).classes('mt-2')
                c.on_click(lambda c=c: save_connection(c))
    if not webpageutils.is_authenticated(request):
        return webpageutils.redirect(ui, '/login')
    layout.create_layout(statics.__webname__ + ' - ' + settingsgen.get_language().connections_page, request, render)
