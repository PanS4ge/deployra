from fastapi import Request
from web import layout
from nicegui import ui
from utils import settingsgen
from utils import webpageutils
from utils import permissions
from utils.encryption import jwthandle
from utils.managers import users
from requests.exceptions import ConnectionError
from json.decoder import JSONDecodeError
import traceback

def create_layout(page_title: str, request: Request, id : str, content_function):
    def add_navigation():
        current_path = request.url.path
        session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))

        with ui.row().classes('items-center'):
            for page in [
                    "vitals",
                    "containers",
                    "images",
                    "networks",
                    "volumes",
                    "delete"
                ]:
                is_active = current_path == f'/connections/{id}/{page}'
                props = '' if is_active else 'flat' + " " + "text-red-500" if page == "delete" else ''

                ui.button(
                    getattr(settingsgen.get_language(), f"connections_{page}"),
                    on_click=lambda _, p=page: webpageutils.redirect(ui, f'/connections/{id}/{p}')
                ).props(props)
        
        try:
            content_function()
        except ConnectionError as e:
            traceback.print_exc()
            with ui.row().classes("inline-grid absolute-center items-center"):
                with ui.row():
                    with ui.column():
                        ui.label(settingsgen.get_language().no_connection).classes('text-h3')
        except JSONDecodeError as e:
            traceback.print_exc()
            with ui.row().classes("inline-grid absolute-center items-center"):
                with ui.row():
                    with ui.column():
                        ui.label(settingsgen.get_language().no_connection).classes('text-h3')

    layout.create_layout(page_title, request, add_navigation)