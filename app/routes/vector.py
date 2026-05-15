import asyncio
import json
import logging

from fastapi import APIRouter, Request, Response

from services.metrics import LOGS_EXECUTOR, Metrics
from utils.logging import run_in_executor_with_context

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/logs")
async def vector_logs(request: Request):
  body = await request.body()
  node = request.headers.get("x-node-name", "")
  client = f"{request.client.host}:{request.client.port}" if request.client else "-"
  line_count = sum(1 for l in body.splitlines() if l.strip())
  logger.info(f"{client} [{node or '-'}] - Received {line_count} lines of logs")

  by_container: dict[str, list] = {}

  for line in body.splitlines():
    line = line.strip()
    if not line:
      continue
    try:
      payload = json.loads(line)
    except Exception:
      continue

    container = payload.get("container_name", "")
    ts = payload.get("timestamp", "")
    message = payload.get("message", "")

    if not container or not node or not ts or not message:
      continue

    by_container.setdefault(container, []).append(
        {
            "hostname": node,
            "ts": ts,
            "stream": payload.get("stream", ""),
            "message": message,
        }
    )

  if by_container:
    await asyncio.gather(
        *[
            run_in_executor_with_context(
                Metrics().write_logs,
                container,
                entries,
                executor=LOGS_EXECUTOR,
            )
            for container, entries in by_container.items()
        ],
        return_exceptions=True,
    )

  return Response(status_code=204)
