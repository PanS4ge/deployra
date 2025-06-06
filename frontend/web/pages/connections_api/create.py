from fastapi import Request

from utils.managers import notifications
from utils.encryption import jwthandle
import statics

from utils import backend_wrapper
from utils import settingsgen
import web.layout as layout
from nicegui import ui
from nicegui import APIRouter
import traceback
from loguru import logger
from utils import webpageutils
@ui.page('/connections/create')
def main_page(request: Request) -> None:
    def check_connection(url, b, foundversion, hostname):
        b.value = settingsgen.get_language().working
        b.disable()
        b.update()
        out = backend_wrapper.check_backend(url.value)
        if out is not None:
            ui.notify(settingsgen.get_language().found + " " + out.json()["id"], color='positive')
            foundversion.value = out.json()["ver"]
            foundversion.update()
            hostname.value = out.json()["host"]
            hostname.update()
        else:
            ui.notify(settingsgen.get_language().notfoundbackend, color='negative')
        b.value = settingsgen.get_language().c_check
        b.enable()
        b.update()
    
    def render():
        session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
        if(session is None):
            ui.run_javascript(' window.cookies.jwt_token = ""; window.location.href = "/login";')
            return
        with ui.row().classes("absolute-center items-center"):
            with ui.column().classes("items-center"):
                with ui.row().classes("items-center"):
                    ui.label(settingsgen.get_language().createnew_page).classes('text-h5')
                b = None
                with ui.row().classes("flex-nowrap w-[200%]"):
                    with ui.card().classes("w-[50%]"):
                        name = ui.input(label=settingsgen.get_language().c_name, placeholder=settingsgen.get_language().c_name).classes('w-[100%]')
                        url = ui.input(label=settingsgen.get_language().c_url, placeholder=settingsgen.get_language().c_url).classes('w-[100%]')
                        ipad = ui.input(label=settingsgen.get_language().c_ipad, placeholder=settingsgen.get_language().c_ipad).classes('w-[100%]')
                        b = ui.button(text=settingsgen.get_language().c_check).classes('w-[100%]')
                        with ui.row().classes("items-center flex-nowrap"):
                            ui.button(settingsgen.get_language().foundversion)
                            foundversion = ui.input().classes('w-[100%]')
                            foundversion.set_enabled(False)
                        with ui.row().classes("items-center flex-nowrap"):
                            ui.button(settingsgen.get_language().hostname)
                            hostname = ui.input().classes('w-[100%]')
                            hostname.set_enabled(False)
                        b.on_click(lambda url=url, b=b, foundversion=foundversion, hostname=hostname: check_connection(url, b, foundversion, hostname))
                    with ui.card().classes("items-center w-[50%]"):
                        ftison = ui.checkbox(text=settingsgen.get_language().turn_on_ftp)
                        ftuser = ui.input(label=settingsgen.get_language().ftp_username, value=session["email"].split("@")[0]).classes('w-[100%]')
                        ftpass = ui.input(label=settingsgen.get_language().ftp_password, password=True).classes('w-[100%]')
                        ftdoma = ui.input(label=settingsgen.get_language().ftp_domain, value="ftp-<" + settingsgen.get_language().maybethisnode + ">." + session["email"].split("@")[1]).classes('w-[100%]')
                        ftport = ui.input(label=settingsgen.get_language().ftp_port, value="21").classes('w-[100%]')
                        ftpavs = ui.input(label=settingsgen.get_language().ftp_pasv_start).classes('w-[100%]')
                        frpave = ui.input(label=settingsgen.get_language().ftp_pasv_end).classes('w-[100%]')
                        ftssle = ui.input(label=settingsgen.get_language().ftp_ssl_email, value=session["email"]).classes('w-[100%]')
                
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
                        back = backend_wrapper.save_backend(url.value, name.value, ipad.value, shared, access)
                        if(back == "False"):
                            logger.error("Failed to save backend")
                            ui.notify(settingsgen.get_language().something_went_wrong, color='negative')
                            c.text = settingsgen.get_language().save
                            c.enable()
                            c.update()
                        sip = backend_wrapper.save_ip(back, ipad.value)
                        if(ftison.value):
                            backend_wrapper.create_ftp(back, ftuser.value, ftpass.value, ftdoma.value, ftport.value, ftpavs.value, frpave.value, ftssle.value)
                        ui.notify(settingsgen.get_language().saved, color='positive')
                        webpageutils.redirect(ui, '/connections')
                        notifications.add_notification(settingsgen.get_language().saved_connection_noti.replace("{VER}", settingsgen.get_storage_type().get_value(statics.db_name + "_connections", back, "servername") + " " + settingsgen.get_storage_type().get_value(statics.db_name + "_connections", back, "serverversion")).replace("{NAME}", name.value))
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
