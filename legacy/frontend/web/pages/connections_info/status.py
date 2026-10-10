from fastapi import Request

from web.pages import dockernavigation
from utils.encryption import jwthandle
import statics
import json

from utils import backend_wrapper
from utils import settingsgen
from utils.managers import users
from utils.permissions import Permission
import web.layout as layout
from nicegui import ui
from nicegui import APIRouter
from utils import backend_wrapper
from utils import webpageutils
from utils.managers import notifications
import math
import time

from loguru import logger
import random

def ensure_1_decimal(x):
    try:
        return math.floor(x * 10) / 10
    except ValueError:
        return x

@ui.page('/connections/{id}')
@ui.page('/connections/{id}/vitals')
def main_page(request: Request, id: str) -> None:
    def am_i_online() -> bool:
        try:
            json.loads(backend_wrapper.connect_to_backend(id).text) # identyfikacja
            json.loads(backend_wrapper.get_vitals(id))         # czy secret jest dobry
            json.loads(backend_wrapper.get_images(id))         # czy docker jest dobry
            # jeszce tu dorzucić nginxa
            return True
        except Exception:
            return False
    
    def win_lose(option_user : str, option_comp : str):
        if(option_user == "r" and option_comp == "rock" or option_user == "p" and option_comp == "paper" or option_user == "s" and option_comp == "scissors"):
            return 2
        if(option_user == "r" and option_comp == "scissors"):
            return 1
        if(option_user == "p" and option_comp == "rock"):
            return 1
        if(option_user == "s" and option_comp == "paper"):
            return 1
        return 0
    
    def play_rps(option : str, rock : ui.button, paper : ui.button, scissors : ui.button, stat : ui.label, rps : ui.label, win : ui.button, draw : ui.button, lose : ui.button, avg : ui.label):
        rock.classes(remove="bg-green-400")
        rock.classes(remove="bg-yellow-400")
        rock.classes(remove="bg-red-400")
        paper.classes(remove="bg-green-400")
        paper.classes(remove="bg-yellow-400")
        paper.classes(remove="bg-red-400")
        scissors.classes(remove="bg-green-400")
        scissors.classes(remove="bg-yellow-400")
        scissors.classes(remove="bg-red-400")
        rock.update()
        paper.update()
        scissors.update()
        
        user = ""
        comp = ""
        
        if(option == "r"):
            rock.classes("bg-green-400")
            user = "🪨"
        elif(option == "p"):
            paper.classes("bg-green-400")
            user = "📜"
        elif(option == "s"):
            scissors.classes("bg-green-400")
            user = "✂️"
            
        rock.update()
        paper.update()
        scissors.update()
        stat.set_text(settingsgen.get_language().rps_asking)
        stat.update()
        czas = time.perf_counter()
        try:
            bak = backend_wrapper.play_rps(id)
        except Exception as e:
            stat.set_text(settingsgen.get_language().rps_walkover)
            stat.update()
            return
        ans = json.loads(bak)["chosen"]
        perf = time.perf_counter() - czas
        stat.set_text(settingsgen.get_language().rps_returnin.replace("{TIME}", str(round(perf, 4))))
        stat.update()
        avg_val = (float(avg.text.replace(settingsgen.get_language().rps_avg.replace("{AVG}", ""), "")) * (int(win.text) + int(draw.text) + int(lose.text)) + perf) / (int(win.text) + int(draw.text) + int(lose.text) + 1)
        avg.set_text(settingsgen.get_language().rps_avg.replace("{AVG}", str(round(avg_val, 4))))
        
        if(ans == "rock"):
            comp = "🪨"
        elif(ans == "paper"):
            comp = "📜"
        elif(ans == "scissors"):
            comp = "✂️"
        
        rps.set_text(settingsgen.get_language().rps_selected.replace("{RPS_U}", user).replace("{RPS_C}", comp))
        rps.update()
        
        wl = win_lose(option, ans)
        rock.classes(remove="bg-green-400")
        rock.classes(remove="bg-yellow-400")
        rock.classes(remove="bg-red-400")
        paper.classes(remove="bg-green-400")
        paper.classes(remove="bg-yellow-400")
        paper.classes(remove="bg-red-400")
        scissors.classes(remove="bg-green-400")
        scissors.classes(remove="bg-yellow-400")
        scissors.classes(remove="bg-red-400")
        rock.update()
        paper.update()
        scissors.update()
        if(wl == 2):
            if(option == "r"):
                rock.classes("bg-yellow-400")
            elif(option == "p"):
                paper.classes("bg-yellow-400")
            elif(option == "s"):
                scissors.classes("bg-yellow-400")
            draw.set_text(int(draw.text) + 1)
            draw.update()
        if(wl == 1):
            if(option == "r"):
                rock.classes("bg-green-400")
                scissors.classes("bg-red-400")
            elif(option == "p"):
                paper.classes("bg-green-400")
                rock.classes("bg-red-400")
            elif(option == "s"):
                scissors.classes("bg-green-400")
                paper.classes("bg-red-400")
            win.set_text(int(win.text) + 1)
            win.update()
        if(wl == 0):
            if(option == "r"):
                rock.classes("bg-red-400")
                scissors.classes("bg-green-400")
            elif(option == "p"):
                paper.classes("bg-red-400")
                rock.classes("bg-green-400")
            elif(option == "s"):
                scissors.classes("bg-red-400")
                paper.classes("bg-green-400")    
            lose.set_text(int(lose.text) + 1)
            lose.update()
        rock.update()
        paper.update()
        scissors.update()
            
    
    @ui.refreshable
    def vitals_elements():
        def ensure_2_char_hex(hex_str: str) -> str:
            if len(hex_str) == 1:
                return "0" + hex_str
            return hex_str
        
        vitals = json.loads(backend_wrapper.get_vitals(id))
        ramperc = float(vitals["ram"])
        cpuperc = float(vitals["cpu"])
        
        with ui.row():
            with ui.card():
                with ui.row().classes("inline-flex"):
                    ui.html('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" class="bi bi-memory" viewBox="0 0 16 16"><path d="M1 3a1 1 0 0 0-1 1v8a1 1 0 0 0 1 1h4.586a1 1 0 0 0 .707-.293l.353-.353a.5.5 0 0 1 .708 0l.353.353a1 1 0 0 0 .707.293H15a1 1 0 0 0 1-1V4a1 1 0 0 0-1-1zm.5 1h3a.5.5 0 0 1 .5.5v4a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5v-4a.5.5 0 0 1 .5-.5m5 0h3a.5.5 0 0 1 .5.5v4a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5v-4a.5.5 0 0 1 .5-.5m4.5.5a.5.5 0 0 1 .5-.5h3a.5.5 0 0 1 .5.5v4a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5zM2 10v2H1v-2zm2 0v2H3v-2zm2 0v2H5v-2zm3 0v2H8v-2zm2 0v2h-1v-2zm2 0v2h-1v-2zm2 0v2h-1v-2z"/></svg>')
                    calculated = int(math.floor(255 * ramperc / 100))
                    color = "#" + ensure_2_char_hex(hex(calculated)[2:]) + ensure_2_char_hex(hex(255 - calculated)[2:]) + "00"
                    with ui.row().classes("inline-grid bg-[" + color + "] w-[" + str(ramperc) + "px] h-[21px]"):
                        pass
                    ui.label(str(ensure_1_decimal(vitals["ram"])) + "%")
                with ui.row().classes("inline-flex"):
                    ui.html('<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" fill="currentColor" class="bi bi-cpu" viewBox="0 0 16 16"><path d="M5 0a.5.5 0 0 1 .5.5V2h1V.5a.5.5 0 0 1 1 0V2h1V.5a.5.5 0 0 1 1 0V2h1V.5a.5.5 0 0 1 1 0V2A2.5 2.5 0 0 1 14 4.5h1.5a.5.5 0 0 1 0 1H14v1h1.5a.5.5 0 0 1 0 1H14v1h1.5a.5.5 0 0 1 0 1H14v1h1.5a.5.5 0 0 1 0 1H14a2.5 2.5 0 0 1-2.5 2.5v1.5a.5.5 0 0 1-1 0V14h-1v1.5a.5.5 0 0 1-1 0V14h-1v1.5a.5.5 0 0 1-1 0V14h-1v1.5a.5.5 0 0 1-1 0V14A2.5 2.5 0 0 1 2 11.5H.5a.5.5 0 0 1 0-1H2v-1H.5a.5.5 0 0 1 0-1H2v-1H.5a.5.5 0 0 1 0-1H2v-1H.5a.5.5 0 0 1 0-1H2A2.5 2.5 0 0 1 4.5 2V.5A.5.5 0 0 1 5 0m-.5 3A1.5 1.5 0 0 0 3 4.5v7A1.5 1.5 0 0 0 4.5 13h7a1.5 1.5 0 0 0 1.5-1.5v-7A1.5 1.5 0 0 0 11.5 3zM5 6.5A1.5 1.5 0 0 1 6.5 5h3A1.5 1.5 0 0 1 11 6.5v3A1.5 1.5 0 0 1 9.5 11h-3A1.5 1.5 0 0 1 5 9.5zM6.5 6a.5.5 0 0 0-.5.5v3a.5.5 0 0 0 .5.5h3a.5.5 0 0 0 .5-.5v-3a.5.5 0 0 0-.5-.5z"/></svg>')
                    calculated = int(math.floor(255 * cpuperc / 100))
                    color = "#" + ensure_2_char_hex(hex(calculated)[2:]) + ensure_2_char_hex(hex(255 - calculated)[2:]) + "00"
                    with ui.row().classes("inline-grid bg-[" + color + "] w-[" + str(cpuperc) + "px] h-[21px]"):
                        pass
                    ui.label(str(ensure_1_decimal(vitals["cpu"])) + "%")
            with ui.card():
                with ui.column():
                    ui.label(settingsgen.get_language().rps_status).classes('text-h5')
                    with ui.row():
                        stat = ui.label(settingsgen.get_language().rps_returnin.replace("{TIME}", ""))
                        avg = ui.label(settingsgen.get_language().rps_avg.replace("{AVG}", "0"))
                        ui.label(settingsgen.get_language().rps_sec)
                    rps = ui.label(settingsgen.get_language().rps_selected.replace("{RPS_U}", "").replace("{RPS_C}", ""))
                    with ui.row():
                        win = ui.button("0", color="positive")
                        draw = ui.button("0", color="warning")
                        lose = ui.button("0", color="negative")
                        
                        win.set_enabled(False)
                        draw.set_enabled(False)
                        lose.set_enabled(False)
                    with ui.row():
                        r = ui.button("🪨")
                        p = ui.button("📄")
                        s = ui.button("✂️")
                        
                        r.on_click(lambda r=r, p=p, s=s, stat=stat, rps=rps, win=win, draw=draw, lose=lose, avg=avg: play_rps("r", r, p, s, stat, rps, win, draw, lose, avg))
                        p.on_click(lambda r=r, p=p, s=s, stat=stat, rps=rps, win=win, draw=draw, lose=lose, avg=avg: play_rps("p", r, p, s, stat, rps, win, draw, lose, avg))
                        s.on_click(lambda r=r, p=p, s=s, stat=stat, rps=rps, win=win, draw=draw, lose=lose, avg=avg: play_rps("s", r, p, s, stat, rps, win, draw, lose, avg))
                        
                        r.props('flat color=black border-2 border-black-500')
                        p.props('flat color=black border-2 border-black-500')
                        s.props('flat color=black border-2 border-black-500')

    def render():
        session = jwthandle.decode_jwt(request.cookies.get('jwt_token'))
        if(session is None):
            ui.run_javascript(' window.cookies.jwt_token = ""; window.location.href = "/login";')
            return
        if(not settingsgen.get_storage_type().does_db_exist(statics.db_name + "_connections")):
            ui.run_javascript('window.location.href = "/connections";')
            return
        if(not settingsgen.get_storage_type().get_value(statics.db_name + "_connections", id, "access_key")):
            # o czym my wogóle tu gadamy?
            return
        with ui.row().classes("inline-grid absolute-center items-center"):
            with ui.row():
                with ui.column():
                    vitals_elements()

    if not webpageutils.is_authenticated(request):
        return webpageutils.redirect(ui, '/login')
    
    dockernavigation.create_layout(statics.__webname__ + ' - ' + settingsgen.get_storage_type().get_value(statics.db_name + "_connections", id, "name"), request, id, render)
    ui.timer(statics.timer_interval, vitals_elements.refresh, immediate=True)