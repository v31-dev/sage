import logging

from fastapi import APIRouter, HTTPException, Request, Response

import config
from services.logs import Logs
from utils.api import read_body
from utils.executor import LOGS_EXECUTOR, run_in_executor_with_context
from utils.logging import NO_ACCESS_LOG

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/logs", openapi_extra=NO_ACCESS_LOG)  # a push every 10 s from each worker
async def vector_logs(request: Request):
  """A worker's Vector pushes its lines here. It sends a batch again on a 5xx, and drops it on a 4xx."""
  hostname = request.headers.get("x-node-name", "")
  if not hostname:
    raise HTTPException(status_code=400, detail="No X-Node-Name.")
  body = await read_body(request, config.LOG_INTAKE_MAX_BYTES, "Logs too large.")
  try:
    skipped = await run_in_executor_with_context(Logs().ingest, hostname, body, executor=LOGS_EXECUTOR)
  except Exception:
    raise HTTPException(status_code=503, detail="The logs could not be stored.")
  if skipped:
    logger.warning(f"Skipped {skipped} log lines from {hostname} that were not events of a container.")
  return Response(status_code=204)
