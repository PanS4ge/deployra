from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse
from utils import settingsgen
from utils import webpageutils

class SetupRedirectMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = str(request.url.path)
        if (path.startswith('/nicegui') or path.startswith('/_')):
            return await call_next(request)

        if (not settingsgen.are_there_settings()) and (not path.startswith('/setup')):
            if webpageutils.is_authenticated(request):
                response = webpageutils.html_mid_response(url='/')
                response.delete_cookie('jwt_token')
                return response
            return RedirectResponse('/setup', status_code=307)

        return await call_next(request)