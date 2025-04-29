from fastapi import Request
from fastapi.responses import HTMLResponse
from nicegui import ui
from loguru import logger
from utils import settingsgen

def is_authenticated(request: Request) -> bool:
    return request.cookies.get('jwt_token', None) is not None

def redirect(ui : ui, url : str) -> None:
    ui.run_javascript('window.location.href = "' + url + '";')
        
def html_mid_response(url : str) -> HTMLResponse:
    return HTMLResponse(content="""
        <html><head>
        <meta http-equiv="refresh" content="0;url=/" />
        </head><body>""" + settingsgen.get_language().redirecting + """</body>
        <script>window.location.href = \"""" + url + """\"</script>
        </html>
    """)