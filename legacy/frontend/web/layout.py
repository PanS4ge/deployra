from nicegui import ui
from fastapi import Request
from utils import gravatar
from utils import webpageutils
import statics
from utils import settingsgen
from utils.encryption import jwthandle
from starlette.responses import RedirectResponse
import os
from loguru import logger
from utils.managers import users
from utils.managers import notifications

def create_layout(page_title: str, request: Request, content_function):
    ui.page_title(page_title)
    
    #ui.add_head_html("""
    #    <link rel="preconnect" href="https://fonts.googleapis.com">
    #    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    #    <link href="https://fonts.googleapis.com/css2?family=Fira+Sans:ital,wght@0,100;0,200;0,300;0,400;0,500;0,600;0,700;0,800;0,900;1,100;1,200;1,300;1,400;1,500;1,600;1,700;1,800;1,900&family=Ubuntu:wght@500&display=swap" rel="stylesheet">                 
    #""")
    
    ui.add_head_html("""
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.3.0/font/bootstrap-icons.css">
    """)
    
    #ui.add_css("""
    #    * {
    #        font-family: "Fira Sans", sans-serif;
    #    }           
    #""")
    
    with ui.header().classes('row items-center bg-blue-600 text-white'):
        with ui.row().classes('items-center gap-2'):
            ui.button(on_click=lambda: left_drawer.toggle(), icon='menu').props('flat color=white')
            ui.label(page_title).classes('text-h6 ml-4')
        
        with ui.row().classes('items-center gap-4 absolute inset-y-0 right-0 pr-2'):
            with ui.button(on_click=lambda: right_drawer.toggle()).props('flat color=white'):
                ui.html('<i class="bi bi-bell-fill"></i>')
            if(request.cookies.get('jwt_token') is None):
                ui.label().props('flat color=white')
            else:
                session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
                if not(session is None):
                    ui.label(users.get_name(session['email'])).props('flat color=white')         
                    ui.image(gravatar.get_gravatar(session['email'], 40)).classes('rounded-full w-10 h-10')   
            #ui.html("<i class=\"bi bi-box-arrow-right\"></i>").on('click', lambda: webpageutils.redirect(ui, '/logout')) \
            #        .props('outline round icon=logout').classes('cursor-pointer rounded-lg w-12 h-12 text-4xl')
            ui.button('', on_click=lambda: webpageutils.redirect(ui, '/logout')).props('outline round icon=logout')

    with ui.left_drawer().classes('bg-white shadow-l') as left_drawer:
        #ui.link(settingsgen.get_language().mainpage_page, '/')
        #ui.link(settingsgen.get_language().settings_page, '/settings')
        with ui.row().on('click', lambda: webpageutils.redirect(ui, '/')):
            ui.label("").classes("bi bi-house-door")
            ui.label(settingsgen.get_language().mainpage_page).classes('ml-2 font-bold')
        with ui.row().on('click', lambda: webpageutils.redirect(ui, '/connections')):
            ui.label("").classes("bi bi-hdd-network")
            ui.label(settingsgen.get_language().connections_page).classes('ml-2 font-bold')
        with ui.row().on('click', lambda: webpageutils.redirect(ui, '/deploy')):
            ui.label("").classes("bi bi-box-seam")
            ui.label(settingsgen.get_language().deploy_page).classes('ml-2 font-bold')
        with ui.row().on('click', lambda: webpageutils.redirect(ui, '/settings')):
            ui.label("").classes("bi bi-gear-fill")
            ui.label(settingsgen.get_language().settings_page).classes('ml-2 font-bold')
            
    with ui.right_drawer(value=False).classes('bg-blue-100') as right_drawer:
        ui.label(settingsgen.get_language().notifications).classes('text-h6 ml-2 font-bold')
        for notif in notifications.read_notifications():
            with ui.row():
                ui.label(notif)

    with ui.column().classes('p-4'):
        content_function()