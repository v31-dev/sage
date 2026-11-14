from services.db.models import Worker
from services.db.writes import get, trim, write
from services.domains.updates import domain_changed, domains_changed
from services.updates import Updates, UpdateSource
from utils.errors import Conflict


def add_worker(hostname: str, subdomain: str | None) -> Worker:
  hostname, subdomain = trim(hostname), trim(subdomain) or None
  if not hostname:
    raise ValueError("hostname is required.")
  if subdomain is None:
    raise ValueError("A worker without a subdomain is not allowed.")
  with write():
    if Worker.select().where(Worker.hostname == hostname).exists():
      raise Conflict(f"Worker {hostname} is already added.")
    if Worker.select().where(Worker.subdomain == subdomain).exists():
      raise Conflict(f"A worker with subdomain {subdomain} already exists.")
    return Worker.create(hostname=hostname, subdomain=subdomain)


def set_worker_subdomain(hostname: str, subdomain: str | None) -> bool:
  """Whether the subdomain changed. The worker's pages are told, and those of its internal and TCP names, which are
  under it."""
  subdomain = trim(subdomain) or None
  if subdomain is None:
    raise ValueError("A worker without a subdomain is not allowed.")
  with write():
    worker = get(Worker, Worker.hostname == hostname)
    if worker.subdomain == subdomain:
      return False
    if Worker.select().where((Worker.subdomain == subdomain) & (Worker.hostname != hostname)).exists():
      raise Conflict(f"A worker with subdomain {subdomain} already exists.")
    worker.subdomain = subdomain
    worker.save()
    domains = list(worker.domains)
  Updates().changed(UpdateSource.WORKER_LIST)
  Updates().changed(UpdateSource.WORKER_ENTRY, hostname)
  for domain in domains:
    domain_changed(domain)
  domains_changed()
  return True


def removal_refusal(worker: Worker) -> str | None:
  """Why the worker can't be removed now: it has containers or internal domains."""
  if worker.containers.exists():
    return "A worker with containers can't be removed."
  if worker.domains.exists():
    return "A worker with internal domains can't be removed."
  return None


def remove_worker(hostname: str):
  with write():
    worker = get(Worker, Worker.hostname == hostname)
    refusal = removal_refusal(worker)
    if refusal is not None:
      raise Conflict(refusal)
    worker.delete_instance()
