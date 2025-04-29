from fastapi import Request
from web import layout
from nicegui import ui
from utils import settingsgen
from utils import webpageutils
from utils import permissions
from utils.encryption import jwthandle
from utils.managers import users

def create_layout(page_title: str, request: Request, content_function):
    def add_navigation():
        current_path = request.url.path
        session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))

        with ui.row().classes('items-center'):
            for page in [
                ("", permissions.Permission.ALLOW_SETTINGS),
                ("user", permissions.Permission.ALLOW_CREATING_USERS_SETTING),
                ("risky", permissions.Permission.ALLOW_RISKY_SETTINGS)
                ]:
                has_permission = session and users.has_permission(session["email"], page[1])
                is_active = current_path == f'/settings/{page[0]}'
                props = '' if is_active else 'flat'
                color = 'negative' if not has_permission else None

                ui.button(
                    getattr(settingsgen.get_language(), f"settings_{page[0]}"),
                    on_click=lambda _, p=page[0]: webpageutils.redirect(ui, f'/settings/{p}')
                ).props(props).props(f'color={color}' if color else '')

        content_function()

    layout.create_layout(page_title, request, add_navigation)