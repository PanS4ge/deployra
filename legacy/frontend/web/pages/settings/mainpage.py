from fastapi import Request, Response

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
from web.pages import settingsnavigation

@ui.page('/settings')
def settings(request: Request) -> None:
    inputs = {}
    
    def handle_save() -> None:
        changes = {}
        for inp in inputs:
            if(inputs[inp].value != settingsgen.read_settings()[inp]):
                changes[inp] = inputs[inp].value
        settingsgen.update_settings(**changes)
        ui.notify(settingsgen.get_language().settings_saved, color='positive')
        
    def render():
        session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
        if session is None:
            ui.run_javascript('window.cookies.jwt_token = ""; window.location.href = "/login";')
            return
        
        if not webpageutils.is_authenticated(request):
            return webpageutils.redirect(ui, '/login')
        
        with ui.row():
            domain = ui.input(
                label=settingsgen.get_language().domain,
                placeholder=settingsgen.get_language().domain,
                value=settingsgen.read_settings()["domain"]
            )
            inputs["domain"] = domain
            rawautodomain = ui.input(
                label=settingsgen.get_language().rawautodomain,
                placeholder=settingsgen.get_language().rawautodomain,
                value=settingsgen.read_settings()["rawautodomain"]
            )
            inputs["rawautodomain"] = rawautodomain
        
        def hide_dns(dnsapi: ui.input) -> None:
            dnsapi.value = ""
            if usedns.value == statics.dns_options[0]:
                if 'hidden' not in dnsapi.classes:
                    dnsapi.classes.append('hidden')
            else:
                if 'hidden' in dnsapi.classes:
                    dnsapi.classes.remove('hidden')
            ui.update(dnsapi)
        
        with ui.row():
            usedns = ui.select(
                options=statics.dns_options,
                label=settingsgen.get_language().dns_provider,
                value=settingsgen.read_settings()["usedns"],
                on_change=lambda: hide_dns(dnsapi)
            )
            inputs["usedns"] = usedns
            dnsapi = ui.input(
                label=settingsgen.get_language().dns_api,
                placeholder=settingsgen.get_language().dns_api,
                value=settingsgen.read_settings()["dnsapi"],
                password=True
            ).classes('hidden' if usedns.value == statics.dns_options[0] else '')      
            inputs["dnsapi"] = dnsapi
            settingsgen.get_dns_provider.cache_clear()
            
        ui.button(
            settingsgen.get_language().save,
            on_click=lambda: handle_save(),
        )

    settingsnavigation.create_layout(
        f"{statics.__webname__} - {settingsgen.get_language().settings_page}",
        request,
        render
    )