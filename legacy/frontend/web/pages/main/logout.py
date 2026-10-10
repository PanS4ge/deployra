from fastapi import Request, Response

from utils.encryption import jwthandle
import statics

from utils import settingsgen
import web.layout as layout
from nicegui import ui
from loguru import logger
from utils.managers import users
from utils import webpageutils

@ui.page('/logout')
def logout(request: Request) -> None:
    if webpageutils.is_authenticated(request):
        response = webpageutils.html_mid_response(url='/login')
        response.delete_cookie('jwt_token')
        return response
    return webpageutils.html_mid_response(url='/')