from fastapi import Request, Response

from utils.encryption import jwthandle
import statics

from utils import settingsgen
import web.layout as layout
from nicegui import ui
from loguru import logger
from utils.managers import users
from utils import webpageutils

@ui.page('/login')
def login(request: Request) -> None:
    def render():
        def try_login() -> Response:
            try:
                if(not users.is_user(username.value)):
                    ui.notify(settingsgen.get_language().wrong_username_or_password, color='negative')
                    return
                if(users.is_password_ok(username.value, password.value)):
                    payload = {
                        'email': username.value
                    }
                    jwt_token = jwthandle.encode_jwt(payload)

                    #response = webpageutils.html_mid_response(url='/')

                    domain = settingsgen.read_settings().get("domain", None)
                    
                    #response.set_cookie(
                    #    key='jwt_token',
                    #    value=jwt_token,
                    #    domain=domain,
                    #    max_age=60 * 60 * 24,
                    #    httponly=True,
                    #    samesite='None' if statics.debug else 'Lax',
                    #    secure=not statics.debug
                    #)

                    logger.info("User logged in successfully")
                    #logger.debug(f"Response headers: {response.headers}")

                    ui.run_javascript("""
                        document.cookie="jwt_token=""" + jwt_token + """;path=/;max-age=""" + str(60 * 60 * 24) + """"; window.location.href = "/";                
                    """)
                else:
                    ui.notify(settingsgen.get_language().wrong_username_or_password, color='negative')
            except Exception as e:
                logger.error(e)
                ui.notify(settingsgen.get_language().something_went_wrong, color='negative')

        if webpageutils.is_authenticated(request):
            webpageutils.redirect(ui, '/')
            return
        with ui.card().classes('absolute-center'):
            username = ui.input(settingsgen.get_language().login_btn).on('keydown.enter', try_login)
            password = ui.input(settingsgen.get_language().pass_btn).props('type=password').on('keydown.enter', try_login)
            ui.button(settingsgen.get_language().login_btn, on_click=try_login)
    layout.create_layout(statics.__webname__ + ' - ' + settingsgen.get_language().login_page, request, render)
