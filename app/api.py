from fastapi import FastAPI

from routes.info import router as info_router
from routes.projects import router as projects_router
from routes.workers import router as workers_router
from scheduler import app as app_rocketry
from utils.logging import LoggedSession, fastapi_middleware

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
app.add_middleware(fastapi_middleware)
app.state.rocketry = LoggedSession(app_rocketry.session)


@app.get("/")
def health():
    return {"status": "ok"}


# Mount routes
app.include_router(info_router, prefix="/api")
app.include_router(workers_router, prefix="/api/workers")
app.include_router(projects_router, prefix="/api/projects")
