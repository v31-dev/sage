from services.db.models import Worker
from services.db.writes import get, trim, write
from services.domains.updates import domain_changed, domains_changed
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
  """Whether the subdomain changed. Its internal and TCP names are under it, so their pages are told."""
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
  for domain in domains:
    domain_changed(domain)
  domains_changed()
  return True


def remove_worker(hostname: str):
  with write():
    worker = get(Worker, Worker.hostname == hostname)
    if worker.containers.exists():
      raise Conflict("A worker with containers can't be removed.")
    if worker.domains.exists():
      raise Conflict("A worker with internal domains can't be removed.")
    worker.delete_instance()
