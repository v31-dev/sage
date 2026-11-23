import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from routes.telegraf import router as telegraf_router
from routes.vector import router as vector_router
from services.tailscale import Tailscale
from utils.api import RefuseOtherSites

logger = logging.getLogger(__name__)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
# Only the workers' Telegraf and Vector call it, never a browser.
app.add_middleware(RefuseOtherSites)


@app.middleware("http")
async def only_at_the_manager_address(request: Request, call_next):
  """Answers only at the manager's tailnet address, the one the workers' configurations name, so a name that another
  page made resolve to it (DNS rebinding) gets nothing."""
  if request.url.hostname != Tailscale().ip():
    logger.warning(f"Refused a request to {request.url.path} sent to {request.url.hostname!r}, not the manager's "
                   "tailnet address.")
    return JSONResponse(status_code=421, content={"detail": "Not the manager's tailnet address."})
  return await call_next(request)


@app.get("/")
def health():
  return {"status": "ok"}


# Mount routes
app.include_router(telegraf_router, prefix="/api/telegraf")
app.include_router(vector_router, prefix="/api/vector")
