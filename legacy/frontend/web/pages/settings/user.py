from fastapi import Request, Response

from web.pages import settingsnavigation
from utils.permissions import Permission, risky_permissions
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

class UserDialog(ui.dialog):
    name : str = ""
    
    def __init__(self, name):
        super().__init__()
        self.name = name
    
    email : str = ""
    uiupdates : dict = {}
    
    def change_email(self, email):
        logger.info(f"Changing email on " + self.name + " to " + email)
        self.email = email
        
    def get_field(self, field : str) -> str:
        if(self.email == ""):
            return ""
        return users.get_field(self.email, field)

@ui.page('/settings/user')
def user(request: Request) -> None:
    
    with ui.dialog() as create_dialog:
        session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
        if session is None:
            ui.run_javascript('window.cookies.jwt_token = ""; window.location.href = "/login";')
            return
        def handle_add_user(name, surname, email, password, password_repeat, permmap):
            permstring = ""
            for perm in Permission:
                if permmap[perm].value:
                    permstring += perm.value
            if password != password_repeat:
                ui.notify(settingsgen.get_language().passwords_dont_match, color='negative')
                return
            if users.is_user(email):
                ui.notify(settingsgen.get_language().user_already_exists, color='negative')
                return
            if not users.save_user(email, password, name, surname, permstring, False):
                ui.notify(settingsgen.get_language().something_went_wrong, color='negative')
                return
            ui.notify(settingsgen.get_language().user_created, color='positive')
        
        with ui.row():
            with ui.column().classes("p-4 border-4 border-solid border-black bg-white shadow-l rounded-[16px]"):
                ui.label(settingsgen.get_language().add_user).classes('text-h6')
                with ui.column().classes('items-center'):
                        with ui.row().classes('items-center'):
                            name = ui.input(
                                label=settingsgen.get_language().name,
                                placeholder=settingsgen.get_language().name
                            )
                            surname = ui.input(
                                label=settingsgen.get_language().surname,
                                placeholder=settingsgen.get_language().surname
                            )
                        email = ui.input(
                            label=settingsgen.get_language().email,
                            placeholder=settingsgen.get_language().email,
                            value="@" + settingsgen.read_settings()["domain"]
                        )
                        password = ui.input(
                            label=settingsgen.get_language().password,
                            placeholder=settingsgen.get_language().password,
                            password=True
                        )
                        password_repeat = ui.input(
                            label=settingsgen.get_language().password_repeat,
                            placeholder=settingsgen.get_language().password_repeat,
                            password=True
                        )
                ui.label(settingsgen.get_language().permissions).classes('text-h6')
                permmap = {}
                risky_perms = users.has_permission(session['email'], Permission.ALLOW_RISKY_PERMISSIONS)
                with ui.row().classes('flex flex-wrap gap-4'):          
                    for perm in Permission:
                        if(not users.has_permission(session["email"], perm)):
                            checkbox = ui.checkbox(
                                text=getattr(settingsgen.get_language(), "p_" + perm.name),
                                value=False
                            ).classes("text-red-500")
                            checkbox.disable()
                            continue
                        if(perm in risky_permissions and not risky_perms):
                            checkbox = ui.checkbox(
                                text=getattr(settingsgen.get_language(), "p_" + perm.name),
                                value=False
                            ).classes("text-red-800")
                            checkbox.disable()
                            continue
                        checkbox = ui.checkbox(
                            text=getattr(settingsgen.get_language(), "p_" + perm.name),
                            value=False
                        )
                        if(perm in risky_permissions):
                            checkbox.classes("text-red-800")
                        permmap[perm] = checkbox
                with ui.row().classes('items-center gap-4 inset-y-0 right-0 pr-2'):
                    ui.button(settingsgen.get_language().cancel, on_click=lambda: create_dialog.close()).props('flat color=red')
                    ui.button(settingsgen.get_language().create_user, on_click=lambda: handle_add_user(name.value, surname.value, email.value, password.value, password_repeat.value, permmap)).props('flat color=green')
    
    with UserDialog("delete dialog") as delete_dialog:
        with ui.column():
            ui.label(settingsgen.get_language().delete_user).classes('text-h6')
    
    with UserDialog("modify dialog") as modify_dialog:
        session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
        if session is None:
            ui.run_javascript('window.cookies.jwt_token = ""; window.location.href = "/login";')
            return
        usesinputs = {}
        def handle_modify_user(email, usesinputs : dict):
            if usesinputs["password"] != "" and usesinputs["password_repeat"] != "" and usesinputs["password"].value != usesinputs["password_repeat"].value:
                ui.notify(settingsgen.get_language().passwords_dont_match, color='negative')
                return
            changes = {}
            for field in usesinputs:
                # BLACKLISTED FIELDS
                if(field == "permmap"):
                    continue
                if(field == "email"):
                    continue
                if(field == "password_repeat"):
                    continue
                # BLACKLISTED FIELDS
                if usesinputs[field].value != users.get_field(email, field):
                    changes[field] = usesinputs[field].value
                # CLEARING CACHE OF NAME
                if(field == "name" or field == "surname"):
                    users.get_name.cache_clear()
                # CLEARING CACHE OF NAME
            permstring = ""
            for perm in Permission:
                if usesinputs["permmap"][perm].value:
                    permstring += perm.value
            if users.is_user(email):
                if not users.modify_user(email, changes) and not users.change_permissions(email, permstring):
                    ui.notify(settingsgen.get_language().something_went_wrong, color='negative')
                    return
                ui.notify(settingsgen.get_language().user_edited, color='positive')
                modify_dialog.close()
        
        with ui.row():
            with ui.column().classes("p-4 border-4 border-solid border-black bg-white shadow-l rounded-[16px]"):
                ui.label(settingsgen.get_language().modify_user).classes('text-h6')
                with ui.column().classes('items-center'):
                    with ui.row().classes('items-center'):
                        name = ui.input(
                            label=settingsgen.get_language().name,
                            placeholder=settingsgen.get_language().name
                        )
                        usesinputs["name"] = name
                        surname = ui.input(
                            label=settingsgen.get_language().surname,
                            placeholder=settingsgen.get_language().surname
                        )
                        usesinputs["surname"] = surname
                    email = ui.input(
                        label=settingsgen.get_language().email,
                        placeholder=settingsgen.get_language().email
                    )
                    usesinputs["email"] = email
                    password = ui.input(
                        label=settingsgen.get_language().password,
                        placeholder=settingsgen.get_language().password,
                        password=True
                    )
                    usesinputs["password"] = password
                    password_repeat = ui.input(
                        label=settingsgen.get_language().password_repeat,
                        placeholder=settingsgen.get_language().password_repeat,
                        password=True
                    )
                    usesinputs["password_repeat"] = password_repeat
                ui.label(settingsgen.get_language().permissions).classes('text-h6')
                permmap = {}
                risky_perms = users.has_permission(session['email'], Permission.ALLOW_RISKY_PERMISSIONS)
                with ui.row().classes('flex flex-wrap gap-4'):          
                    for perm in Permission:
                        if(not users.has_permission(session["email"], perm)):
                            checkbox = ui.checkbox(
                                text=getattr(settingsgen.get_language(), "p_" + perm.name),
                                value=False
                            ).classes("text-red-500")
                            checkbox.disable()
                            continue
                        if(perm in risky_permissions and not risky_perms):
                            checkbox = ui.checkbox(
                                text=getattr(settingsgen.get_language(), "p_" + perm.name),
                                value=False
                            ).classes("text-red-800")
                            checkbox.disable()
                            continue
                        checkbox = ui.checkbox(
                            text=getattr(settingsgen.get_language(), "p_" + perm.name),
                            value=False
                        )
                        if(perm in risky_permissions):
                            checkbox.classes("text-red-800")
                        permmap[perm] = checkbox
                    modify_dialog.uiupdates = usesinputs
                    usesinputs["permmap"] = permmap
                with ui.row().classes('items-center gap-4 inset-y-0 right-0 pr-2'):
                    ui.button(settingsgen.get_language().cancel, on_click=lambda: create_dialog.close()).props('flat color=red')
                    ui.button(settingsgen.get_language().modify_user, on_click=lambda: handle_modify_user(email.value, usesinputs)).props('flat color=green')
    
    
    def delete_open(user):
        delete_dialog.open()
        
    def modify_open(user):
        modify_dialog.change_email(user)
        modify_dialog.uiupdates["name"].value = modify_dialog.get_field("name")
        ui.update(modify_dialog.uiupdates["name"])
        modify_dialog.uiupdates["surname"].value = modify_dialog.get_field("surname")
        ui.update(modify_dialog.uiupdates["surname"])
        modify_dialog.uiupdates["email"].value = modify_dialog.email
        ui.update(modify_dialog.uiupdates["email"])
        for perm in modify_dialog.uiupdates["permmap"]:
            modify_dialog.uiupdates["permmap"][perm].value = users.has_raw_permission(modify_dialog.email, perm)
            ui.update(modify_dialog.uiupdates["permmap"][perm])
        modify_dialog.open()

    def create_open():
        create_dialog.open()

    def render():
        session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
        if session is None:
            ui.run_javascript('window.cookies.jwt_token = ""; window.location.href = "/login";')
            return

        with ui.row().classes('flex flex-wrap gap-4'):
            for user in users.get_all_users():
                perm_color = "bg-yellow-500" if users.has_permission(user, Permission.FULL_ACCESS) else "bg-green-100"
                with ui.row().classes('rounded-2xl border-4 border-solid border-black ' + perm_color + ' shadow-l ps-4'):
                    with ui.row().classes('rounded-2xl bg-white shadow-l p-4'):
                        with ui.column():
                            ui.label(user).classes('text-h6')
                            ui.label(users.get_name(user))
                        with ui.row().classes('items-center gap-4 inset-y-0 right-0 pr-2'):
                            if (users.has_permission(session["email"], Permission.ALLOW_MODIFYING_USERS)):
                                with ui.button().classes("p-1 rounded flex items-center justify-center ml-auto").on("click", lambda _, u=user: modify_open(u)):
                                    ui.html('<i class="bi bi-pencil-square text-[24px]"></i>')
                            if (users.has_permission(session["email"], Permission.ALLOW_DELETING_USERS) and not users.is_deletable(user)):
                                with ui.button().classes("p-1 rounded flex items-center justify-center ml-auto").on("click", lambda _, u=user: delete_open(u)):
                                    ui.html('<i class="bi bi-trash text-[24px]"></i>')
            
            if(users.has_permission(session["email"], Permission.ALLOW_CREATING_USERS_SETTING)):
                with ui.row().classes('rounded-2xl border-4 border-solid border-black shadow-l grid items-center justify-center bg-white w-[364.05px] h-[109px]'):
                    with ui.button().classes("p-1 rounded flat flex items-center justify-center ml-auto rounded-full").on("click", lambda _: create_open()):
                        ui.html('<i class="bi bi-plus-circle text-[24px]"></i> ' + settingsgen.get_language().add_user)

    if not webpageutils.is_authenticated(request):
        return webpageutils.redirect(ui, '/login')

    settingsnavigation.create_layout(
        f"{statics.__webname__} - {settingsgen.get_language().settings_page}",
        request,
        render
    )