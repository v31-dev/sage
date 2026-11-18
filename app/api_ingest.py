from fastapi import FastAPI

from routes.telegraf import router as telegraf_router
from routes.vector import router as vector_router
from utils.api import RefuseOtherSites

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
# Only the workers' Telegraf and Vector call it, never a browser.
app.add_middleware(RefuseOtherSites)


@app.get("/")
def health():
  return {"status": "ok"}


# Mount routes
app.include_router(telegraf_router, prefix="/api/telegraf")
app.include_router(vector_router, prefix="/api/vector")
