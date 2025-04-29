from fastapi import Request, Response

from web.pages import settingsnavigation
from utils.permissions import Permission
from utils.encryption import jwthandle
import statics

from utils import settingsgen
import web.layout as layout

import os
import random
import time

from nicegui import ui, app
from loguru import logger
from utils.managers import users
from utils import webpageutils

@ui.page('/settings/risky')
def settings(request: Request) -> None:
    def handle_factory_reset():
        session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
        if session is None or not users.has_permission(session["email"], Permission.ALLOW_FULL_RISKY_SETTINGS):
            return

        for db in settingsgen.get_storage_type().get_all_databases():
            b = settingsgen.get_storage_type().delete_db(db)
            if(b):
                logger.success(f"Database {db} deleted")
            else:
                logger.critical(f"Database {db} could not be deleted")

        for keyfile in [statics.private_key, statics.public_key]:
            if os.path.exists(keyfile):
                os.remove(keyfile)
                logger.success(f"{os.path.basename(keyfile)} deleted")

        key_dir = os.path.dirname(statics.private_key)
        if os.path.exists(key_dir):
            os.rmdir(key_dir)
            logger.success("Key directory deleted")

        if settingsgen.are_there_settings():
            os.remove(os.path.join(os.getcwd(), "settings.json"))
            logger.success("Settings deleted")

        ui.notify(settingsgen.get_language().factory_reset_done, color='positive')

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
                        handle_factory_reset()
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

    # -------- RENDER PAGE --------
    def render():
        session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
        if session is None:
            ui.run_javascript('window.cookies.jwt_token = ""; window.location.href = "/login";')
            return

        with ui.column():
            if users.has_permission(session["email"], Permission.ALLOW_RISKY_SETTINGS):
                with ui.row():
                    ui.button(settingsgen.get_language().factory_reset)\
                        .props('outline color=negative')\
                        .on('click', lambda _: dialog.open())

    if not webpageutils.is_authenticated(request):
        return webpageutils.redirect(ui, '/login')

    settingsnavigation.create_layout(
        f"{statics.__webname__} - {settingsgen.get_language().settings_page}",
        request,
        render
    )