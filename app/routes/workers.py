from fastapi import APIRouter, HTTPException
from typing import Literal

from utils.common import get_env
from services.db import Worker
from services.metrics import Metrics
from services.tailscale import Tailscale


router = APIRouter()

@router.get("/")
def get():
  return list(Worker.select().dicts())

@router.get("/{hostname}/metrics")
def get_metrics(hostname: str, period: Literal['1h', '24h', '1w', '1m'] = '1h'):
  worker = Worker.select().where(Worker.hostname == hostname).first()
  
  if not worker and not get_env("HOSTNAME") == hostname:
    raise HTTPException(status_code=404, detail="System not found")

  data = Metrics().query_period(hostname, period)
  
  if worker:
    data['meta']['ip'] = worker.ip
  elif hostname == get_env("HOSTNAME"):
    data['meta']['ip'] = Tailscale().ip()
  
  return data

@router.get("/{hostname}/logs/{container}")
def get_logs(hostname: str, container: str, search: str = '', from_ts: str = '', to_ts: str = ''):
  if hostname == '*':
    hostname = ''
  else:
    worker = Worker.select().where(Worker.hostname == hostname).first()

    if not worker and not get_env("HOSTNAME") == hostname:
      raise HTTPException(status_code=404, detail="System not found")

  try:
    return Metrics().query_logs(container, hostname=hostname, search=search, from_ts=from_ts, to_ts=to_ts)
  except Exception as e:
    raise HTTPException(status_code=400, detail=f"Invalid search query: {str(e)}")