from fastapi import FastAPI

from routes.vector import router as vector_router
from utils.logging import fastapi_middleware

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
app.add_middleware(fastapi_middleware)


@app.get("/")
def health():
  return {"status": "ok"}


# Mount routes
app.include_router(vector_router, prefix="/api/vector")
