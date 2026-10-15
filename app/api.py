import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.routing import Route
from starlette.staticfiles import StaticFiles

from routes.backups import router as backup_router
from routes.info import router as info_router
from routes.notifications import router as notifications_router
from routes.projects import router as projects_router
from routes.settings import router as settings_router
from routes.tasks import router as tasks_router
from routes.workers import router as workers_router
from services.cloudflare import Cloudflare
from services.manager import Manager
from utils.logging import fastapi_middleware

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
app.add_middleware(fastapi_middleware)

# While Sage is paused or stopping, only reads are served: GETs, the POST routes that only
# read, and pause/resume themselves.
_ALLOWED_WHILE_PAUSED = ("/api/settings/pause", "/api/settings/resume")
_READ_ONLY_POSTS = ("/get_available_workers", "/metrics", "/platform_backup_status", "/test")


@app.middleware("http")
async def refuse_changes_unless_running(request: Request, call_next):
  state = Manager().task_state()
  if (state != "running"
          and request.method not in ("GET", "HEAD", "OPTIONS")
          and request.url.path not in _ALLOWED_WHILE_PAUSED
          and not request.url.path.endswith(_READ_ONLY_POSTS)):
    detail = "Sage is paused; resume it first." if state == "paused" else "Sage is stopping."
    return JSONResponse(status_code=503, content={"detail": detail})
  return await call_next(request)


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
  return JSONResponse(status_code=400, content={"detail": str(exc)})

static_dir = Path("/app/static")


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

if (static_dir / "index.html").is_file():
  app.mount("/", SpaStaticFiles(directory=static_dir, html=False, check_dir=False), name="ui")
else:
  @app.get("/")
  def health():
    return {"status": "ok"}


async def redirect_to_manager(request: Request):
  return RedirectResponse(
      str(request.url.replace(scheme="https", netloc=Cloudflare().manager_hostname)), status_code=308)

# Plain HTTP only sends browsers to the manager's address.
redirect_app = Starlette(routes=[Route("/{path:path}", redirect_to_manager)])

# In production the UI and API answer only at the manager's address https://TS_NAME.core.domain
# Other names for it redirect (the tailnet name or IP, after a certificate warning)
if os.getenv("ENV") != "development":
  @app.middleware("http")
  async def only_manager_address(request: Request, call_next):
    if (request.url.hostname or "").lower() != Cloudflare().manager_hostname.lower():
      return await redirect_to_manager(request)
    return await call_next(request)
