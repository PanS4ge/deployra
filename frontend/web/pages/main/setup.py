from fastapi import Request

import statics
from utils.managers import users
from utils import backend_wrapper, settingsgen
from utils import webpageutils
from utils import checksql
from utils import diffiehellman
from nicegui import ui
import string
from loguru import logger
import web.layout as layout
import random

def domain_no_subdomain(domain: str) -> str:
    if not(all(char in string.ascii_letters for char in domain.split('.')[-1])): return statics.example_domain
    try:
        return ".".join(domain.split('.')[-2:]).split(":")[0]
    except:
        return statics.example_domain

@ui.page('/setup')
def setup(request: Request) -> None:
    def render():
        def handle_save() -> None:
            logger.info("Saving settings...")

            if settingsgen.are_there_settings():
                logger.error("There are settings")
                ui.notify(settingsgen.get_language().settings_already_exist, color='negative')
                webpageutils.redirect(ui, '/')
                return  # zatrzymaj funkcję

            if password.value != password_repeat.value:
                logger.error("Password not the same")
                ui.notify(settingsgen.get_language().passwords_dont_match, color='negative')
                s.loading = False
                s.update()
                return

            ui.notify(settingsgen.get_language().settings_saved, color='positive')

            settingsgen.generate_settings(
                domain=domain.value,
                rawautodomain=rawautodomain.value,
                language=language.value,
                usedns=usedns.value,
                dnsapi=dnsapi.value,
                usestorage=usestorage.value,
                mysql_user=mysql_user.value,
                mysql_password=mysql_password.value,
                mysql_host=mysql_host.value,
                mysql_database=mysql_database.value,
                gravatardefault=statics.gravatar_default_default
            )
            settingsgen.generate_cryptography(
                diffiehellman_priv=diffiehellman.generate_private_key(diffiehellman.get_private_value(domain.value)),
                diffiehellman_gen=diffiehellman.get_private_value(usestorage.value),
                diffiehellman_prime=random.choice([2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71, 73, 79, 83, 89, 97])
            )

            users.save_user(user=email.value, password=password.value, name=name.value, surname=surname.value, permissions="0", undeletable=True)
            settingsgen.get_language.cache_clear()
            logger.success("Saved settings")
            
            logger.info("Checking if there is a local backend")
            if(backend_wrapper.check_backend("127.0.0.1:" + str(statics.default_backend_port)) is None):
                logger.error("There is no local backend, user will add everything themself")
            else:
                logger.info("There is a local backend, trying to exchange keys")
                temp = backend_wrapper.exchange_dh("127.0.0.1:" + str(statics.default_backend_port))
                shared = temp[0]
                access = temp[1]
                if(statics.debug):
                    backend_wrapper.exchange_dh.cache_clear()
                if not(shared is None):
                    back = backend_wrapper.save_backend("127.0.0.1:" + str(statics.default_backend_port), "local", shared, access)
                    if(back):
                        logger.success("Saved local backend")
                    else:
                        logger.error("It is wrong, keys where secured and doesn't want to give vitals for saving?")
                else:
                    logger.error("Roadblock, Backend doesn't accept new keys, it's already added somewhere.")

            return webpageutils.html_mid_response('/')

        def hide_dns(dnsemail: ui.input, dnsapi: ui.input) -> None:
            dnsemail.value = ""
            dnsapi.value = ""
            if usedns.value == statics.dns_options[0]:
                if 'hidden' not in dnsapi.classes:
                    dnsemail.classes.append('hidden')
                    dnsapi.classes.append('hidden')
            else:
                if 'hidden' in dnsapi.classes:
                    dnsemail.classes.remove('hidden')
                    dnsapi.classes.remove('hidden')
            ui.update(dnsemail)
            ui.update(dnsapi)

        def hide_storage(card: ui.card) -> None:
            if usestorage.value != statics.storage_options[2]:
                if 'hidden' not in card.classes:
                    card.classes.append('hidden')
            else:
                if 'hidden' in card.classes:
                    card.classes.remove('hidden')
            ui.update(card)

        if not settingsgen.are_there_settings():
            with ui.column().classes('absolute-center items-center'):
                with ui.row().classes('items-center'):
                    ui.label(settingsgen.get_language().setup_page).classes('text-2xl font-medium')

                with ui.row().classes('items-center'):
                    language = ui.select(
                        options=statics.possible_languages,
                        label=settingsgen.get_language().language,
                        value=statics.possible_languages[0]
                    )

                with ui.row().classes('items-center'):
                    domain = ui.input(
                        label=settingsgen.get_language().domain,
                        placeholder=settingsgen.get_language().domain,
                        value=request.headers.get('Host').split(':')[0]
                    )
                    rawautodomain = ui.input(
                        label=settingsgen.get_language().rawautodomain,
                        placeholder=settingsgen.get_language().rawautodomain,
                        value="*." + domain_no_subdomain(request.headers.get('Host'))
                    )
                
                ui.label(settingsgen.get_language().dns_provider_make_sure)
                with ui.row().classes('items-center'):
                    usedns = ui.select(
                        options=statics.dns_options,
                        label=settingsgen.get_language().dns_provider,
                        value=statics.dns_options[0],
                        on_change=lambda: hide_dns(dnsemail, dnsapi)
                    )
                    dnsemail = ui.input(
                        label=settingsgen.get_language().dns_email,
                        placeholder=settingsgen.get_language().dns_email
                    ).classes('hidden')
                    dnsapi = ui.input(
                        label=settingsgen.get_language().dns_api,
                        placeholder=settingsgen.get_language().dns_api
                    ).classes('hidden')

                ui.label(settingsgen.get_language().cant_change_later)
                with ui.row().classes('items-center'):
                    usestorage = ui.select(
                        options=statics.storage_options,
                        label=settingsgen.get_language().storage,
                        value=statics.storage_options[0],
                        on_change=lambda: hide_storage(card)
                    )

                def check_db() -> None:
                    if(checksql.check_database_connection(mysql_user.value, mysql_password.value, mysql_host.value, mysql_database.value)):
                        ui.notify(settingsgen.get_language().connection_ok, color='positive')
                    else:
                        ui.notify(settingsgen.get_language().connection_error, color='negative')

                with ui.row().classes('items-center'):
                    card = ui.card().classes('items-center hidden')
                    with card:
                        with ui.row().classes('items-center'):
                            mysql_user = ui.input(label=settingsgen.get_language().mysql_user)
                            mysql_password = ui.input(label=settingsgen.get_language().mysql_password, password=True)
                            mysql_host = ui.input(label=settingsgen.get_language().mysql_host)
                            mysql_database = ui.input(label=settingsgen.get_language().mysql_database)
                            ui.button(settingsgen.get_language().check, on_click=lambda: check_db())

                with ui.row().classes('items-center'):
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
                            value="admin@" + domain_no_subdomain(request.headers.get('Host'))
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

                with ui.row().classes('items-center'):
                    s = ui.button(settingsgen.get_language().save)
                    s.on('click', lambda _: handle_save())
        else:
            ui.notify(settingsgen.get_language().settings_already_exist, color='negative')
            ui.label(settingsgen.get_language().settings_already_exist).classes('text-2xl font-medium')
            webpageutils.redirect(ui, '/')
            return  # zatrzymaj render po redirect

    layout.create_layout(
        statics.__webname__ + ' - ' + settingsgen.get_language().setup_page,
        request,
        render
    )