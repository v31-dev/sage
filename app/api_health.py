from fastapi import FastAPI

from utils.api import RefuseOtherSites
from utils.logging import NO_ACCESS_LOG

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
# Platforms probe it from outside the container; no browser calls it.
app.add_middleware(RefuseOtherSites)


@app.get("/health", openapi_extra=NO_ACCESS_LOG)  # a platform's probe, every few seconds
def health():
  """The process runs: answered from its first second, while the Cloudflare setup and the certificate are done."""
  return {"status": "ok"}
