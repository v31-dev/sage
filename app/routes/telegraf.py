import json
import logging
import zlib

from fastapi import APIRouter, HTTPException, Request, Response

import config
from services.db import Worker
from services.manager import Manager
from services.metrics import Metrics
from services.reports import Reports, parse_batch

logger = logging.getLogger(__name__)
router = APIRouter()


async def _read_body(request: Request) -> bytes:
  """The request body, decompressed. Telegraf sends gzip, chunked (no Content-Length)."""
  chunks = []
  size = 0
  async for chunk in request.stream():
    size += len(chunk)
    if size > config.REPORT_MAX_BYTES:
      raise HTTPException(status_code=413, detail="Report too large.")
    chunks.append(chunk)
  body = b"".join(chunks)

  if request.headers.get("content-encoding", "").lower() == "gzip":
    try:
      body = zlib.decompressobj(wbits=zlib.MAX_WBITS | 16).decompress(body, config.REPORT_MAX_BYTES + 1)
    except zlib.error as e:
      raise HTTPException(status_code=400, detail=f"Bad gzip body: {e}")
    if len(body) > config.REPORT_MAX_BYTES:
      raise HTTPException(status_code=413, detail="Report too large.")
  return body


@router.post("/report")
async def telegraf_report(request: Request):
  hostname = request.headers.get("x-node-name", "")
  try:
    known = Worker.select().where(Worker.hostname == hostname).exists()
  except ValueError:  # the model refuses a name that no worker can have
    known = False
  if not known:
    if Reports().first_unknown(hostname):
      logger.warning(f"Ignoring reports from unknown worker {hostname!r}.")
    raise HTTPException(status_code=404, detail="Unknown worker.")

  body = await _read_body(request)
  try:
    snapshots = parse_batch(json.loads(body))
  except ValueError as e:
    logger.warning(f"Rejected a malformed report from {hostname}: {e}")
    raise HTTPException(status_code=400, detail="Malformed report.")

  arrival = Reports().accept(hostname, snapshots)
  if arrival is not None and arrival.resumed:
    logger.info(f"Receiving reports from {hostname}.")
  Metrics().ingest(hostname, snapshots)
  if arrival is not None and arrival.changed:
    Manager().request_status_sync_for_containers(hostname, arrival.changed)
  return Response(status_code=204)
