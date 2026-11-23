import logging
import zlib
from collections.abc import Callable

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers

logger = logging.getLogger(__name__)


async def read_body(request: Request, max_bytes: int, too_large: str) -> bytes:
  """The body of a push from a worker, decompressed (Telegraf sends gzip, chunked, with no Content-Length): a 413 with
  `too_large` above `max_bytes`."""
  chunks = []
  size = 0
  async for chunk in request.stream():
    size += len(chunk)
    if size > max_bytes:
      raise HTTPException(status_code=413, detail=too_large)
    chunks.append(chunk)
  body = b"".join(chunks)

  if request.headers.get("content-encoding", "").lower() == "gzip":
    try:
      body = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16).decompress(body, max_bytes + 1)
    except zlib.error as e:
      raise HTTPException(status_code=400, detail=f"Bad gzip body: {e}")
    if len(body) > max_bytes:
      raise HTTPException(status_code=413, detail=too_large)
  return body


class RefuseOtherSites:
  """Refuses what another site's page sends, by any method, over HTTP or a WebSocket. The browser says whose page sent
  a request (`Sec-Fetch-Site`), or an older one gives its `Origin`; a link followed from another site to a page passes.
  `origin` gives the one origin whose pages may call; without it, every request a browser sends is refused. A request
  with neither header is not a browser's and passes."""

  def __init__(self, app, origin: Callable[[], str] | None = None):
    self.app = app
    self.origin = origin

  async def __call__(self, scope, receive, send):
    if scope["type"] not in ("http", "websocket"):
      return await self.app(scope, receive, send)
    headers = Headers(scope=scope)
    site = headers.get("sec-fetch-site")
    own = self.origin() if self.origin else None
    if site is not None:
      followed_link = (scope["type"] == "http" and scope["method"] == "GET"
                       and headers.get("sec-fetch-mode") == "navigate" and headers.get("sec-fetch-dest") == "document")
      allowed = own is not None and (site in ("same-origin", "none") or followed_link)
    else:
      allowed = headers.get("origin") in (None, own)
    if allowed:
      return await self.app(scope, receive, send)

    logger.warning(f"Refused a request from another site to {scope['path']} "
                   f"(Sec-Fetch-Site {site!r}, Origin {headers.get('origin')!r}).")
    if scope["type"] == "http":
      response = JSONResponse(status_code=403, content={"detail": "Requests from other sites are refused."})
      await response(scope, receive, send)
    else:
      await send({"type": "websocket.close", "code": 1008})
