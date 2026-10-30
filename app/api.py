import os

import socketio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.applications import Starlette
from starlette.datastructures import URL
from starlette.exceptions import HTTPException
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

import config
from routes.backups import router as backup_router
from routes.info import router as info_router
from routes.notifications import router as notifications_router
from routes.projects import router as projects_router
from routes.settings import router as settings_router
from routes.tasks import router as tasks_router
from routes.updates import TOPICS, sio
from routes.workers import router as workers_router
from services.cloudflare import Cloudflare
from services.manager import Manager
from services.updates import Updates

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

# While Sage is paused, restoring or stopping, only reads are served: GETs, and pause/resume themselves.
_ALLOWED_WHILE_PAUSED = ("/api/settings/pause", "/api/settings/resume")


@app.middleware("http")
async def refuse_changes_unless_running(request: Request, call_next):
  state = Manager().task_state()
  if (state != "running"
          and request.method not in ("GET", "HEAD", "OPTIONS")
          and request.url.path not in _ALLOWED_WHILE_PAUSED):
    detail = {
        "paused": "Sage is paused; resume it first.",
        "restoring": "Sage is restoring a backup; wait for it to finish.",
    }.get(state, "Sage is stopping.")
    return JSONResponse(status_code=503, content={"detail": detail})
  return await call_next(request)


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
  return JSONResponse(status_code=400, content={"detail": str(exc)})


class SpaStaticFiles(StaticFiles):
  async def get_response(self, path: str, scope):
    if path == "api" or path.startswith("api/"):
      raise HTTPException(status_code=404)

    try:
      return await super().get_response(path, scope)
    except HTTPException as exc:
      if exc.status_code != 404:
        raise
      return await super().get_response("index.html", scope)


# Mount routes
app.include_router(info_router, prefix="/api")
app.include_router(backup_router, prefix="/api/backups")
app.include_router(settings_router, prefix="/api/settings")
app.include_router(workers_router, prefix="/api/workers")
app.include_router(projects_router, prefix="/api/projects")
app.include_router(notifications_router, prefix="/api/notifications")
app.include_router(tasks_router, prefix="/api/tasks")

if (config.STATIC_DIR / "index.html").is_file():
  app.mount("/", SpaStaticFiles(directory=config.STATIC_DIR, html=False, check_dir=False), name="ui")
else:
  @app.get("/")
  def health():
    return {"status": "ok"}


async def redirect_to_manager(request: Request):
  return RedirectResponse(
      str(request.url.replace(scheme="https", netloc=Cloudflare().manager_hostname)), status_code=308)

# Plain HTTP only sends browsers to the manager's address.
app_redirect = Starlette(routes=[Route("/{path:path}", redirect_to_manager)])


class OnlyManagerAddress:
  """Answers only at the manager's address: another name gets a redirect to it, or for a WebSocket a refusal."""

  def __init__(self, app):
    self.app = app

  async def __call__(self, scope, receive, send):
    if scope["type"] in ("http", "websocket") and not Cloudflare().is_manager_hostname(URL(scope=scope).hostname):
      if scope["type"] == "http":
        await (await redirect_to_manager(Request(scope, receive)))(scope, receive, send)
      else:
        await send({"type": "websocket.close", "code": 1008})
      return
    await self.app(scope, receive, send)


def build_asgi_app(only_manager_address: bool):
  """What the servers serve: the Socket.IO server in front of the API, and in production only at the manager's
  address https://TS_NAME.core.domain, other names for it redirecting (the tailnet name or IP, after a certificate
  warning)."""
  Updates().serve(TOPICS)
  served = socketio.ASGIApp(sio, other_asgi_app=app, socketio_path="api/socket.io")
  return OnlyManagerAddress(served) if only_manager_address else served


app_asgi = build_asgi_app(os.getenv("ENV") != "development")
