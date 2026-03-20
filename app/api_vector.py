from fastapi import FastAPI

from utils.logging import fastapi_middleware
from routes.vector import router as vector_router


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
app.add_middleware(fastapi_middleware)

@app.get("/")
def health():
    return {"status": "ok"}

# Mount routes
app.include_router(vector_router, prefix="/api/vector")