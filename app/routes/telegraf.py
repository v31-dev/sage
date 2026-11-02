import json
import logging

from fastapi import APIRouter, HTTPException, Request, Response

import config
from services.manager import Manager
from services.metrics import Metrics
from services.worker_reports import WorkerReports, parse_batch
from utils.api import read_body
from utils.logging import NO_ACCESS_LOG

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/report", openapi_extra=NO_ACCESS_LOG)  # a report every few seconds from each worker
async def telegraf_report(request: Request):
  hostname = request.headers.get("x-node-name", "")
  if Manager().worker_entry(hostname) is None:
    if WorkerReports().first_unknown(hostname):
      logger.warning(f"Ignoring reports from unknown worker {hostname!r}.")
    raise HTTPException(status_code=404, detail="Unknown worker.")

  body = await read_body(request, config.REPORT_MAX_BYTES, "Report too large.")
  try:
    snapshots = parse_batch(json.loads(body))
  except ValueError as e:
    logger.warning(f"Rejected a malformed report from {hostname}: {e}")
    raise HTTPException(status_code=400, detail="Malformed report.")

  arrival = WorkerReports().accept(hostname, snapshots)
  if arrival is not None and arrival.resumed:
    logger.info(f"Receiving reports from {hostname}.")
  Metrics().ingest(hostname, snapshots)
  if arrival is not None and arrival.changed:
    Manager().request_status_sync_for_containers(hostname, arrival.changed)
  if arrival is not None:
    Manager().update_worker(hostname, report=arrival.received)
  return Response(status_code=204)
