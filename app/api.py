from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.staticfiles import StaticFiles

from routes.info import router as info_router
from routes.backups import router as backup_router
from routes.settings import router as settings_router
from routes.projects import router as projects_router
from routes.workers import router as workers_router
from routes.notifications import router as notifications_router
from routes.tasks import router as tasks_router
from utils.logging import fastapi_middleware

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
app.add_middleware(fastapi_middleware)

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
