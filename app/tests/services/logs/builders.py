"""What the logs are given: a worker's Vector events (Traefik's access lines among them), an application's lines and
a task's lines, at times that sort with their index."""
import json
from datetime import datetime, timedelta, timezone


def stamp(index):
  """A timestamp that sorts with `index`, in the form Docker gives (nanoseconds)."""
  return f"2026-01-{1 + index // 86400:02d}T{(index // 3600) % 24:02d}:{(index // 60) % 60:02d}:{index % 60:02d}.000000000Z"


def moment(index):
  """The time of `stamp(index)`."""
  return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index)


def lines(first, count, host="w1", word="line"):
  return [{"hostname": host, "ts": stamp(index), "stream": "stdout",
           "message": f"{word}{index} GET /api/v1/items/{index} status=200 latency={index % 97}ms upstream=app"}
          for index in range(first, first + count)]


def task_entries(first, count, task="t1", sub=""):
  return [{"task_id": task, "sub_task_id": sub, "ts": stamp(index), "level": "INFO", "logger": "tests.task",
           "message": f"step {index} of the task, pulling and starting the containers"}
          for index in range(first, first + count)]


def event(container, message, index=0, stream="stdout"):
  """A line as a worker's Vector pushes it."""
  return {"container_name": container, "timestamp": stamp(index), "stream": stream, "message": message}


def access_line(index=0, **fields):
  """One of Traefik's access lines, as its JSON access log writes it."""
  return json.dumps({"ClientHost": "100.64.0.9", "DownstreamContentSize": 1543, "DownstreamStatus": 200,
                     "Duration": 12_500_000, "RequestHost": "web.example.com", "RequestMethod": "GET",
                     "RequestPath": f"/items/{index}?page=2", "RouterName": "demo-web@file",
                     "ServiceName": "demo-web@file", "StartUTC": stamp(index), "entryPointName": "websecure",
                     "level": "info", "msg": "", **fields})


def push(*events) -> bytes:
  return "\n".join(each if isinstance(each, str) else json.dumps(each) for each in events).encode()


def request_at(index, **fields):
  return event("traefik", access_line(index, **fields), index)
