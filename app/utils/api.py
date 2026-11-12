import zlib

from fastapi import HTTPException, Request


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
