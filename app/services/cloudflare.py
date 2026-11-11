import logging
from datetime import timezone
from email.utils import parsedate_to_datetime

import httpx

import config
from services.base import Base
from services.tailscale import Tailscale
from utils.common import dict_to_obj, get_env, utcnow
from utils.retry import retry

logger = logging.getLogger(__name__)


def dns_record_matches(record: dict, desired: dict) -> bool:
  return all(record.get(key) == value for key, value in desired.items())


class CloudflareError(Exception):
  """The Cloudflare API refused a request; carries the API's own error messages."""

  def __init__(self, status_code: int, reasons: str, retry_after: str | None = None):
    self.status_code = status_code
    self.transient = status_code >= 500 or status_code == 429
    self.retry_after_seconds = None
    if retry_after:
      retry_after = retry_after.strip()
      try:
        if retry_after.isascii() and retry_after.isdecimal():
          self.retry_after_seconds = int(retry_after)
        else:
          retry_at = parsedate_to_datetime(retry_after)
          if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
          self.retry_after_seconds = max(0, (retry_at.astimezone(timezone.utc).replace(tzinfo=None) - utcnow()).total_seconds())
      except (ValueError, TypeError, OverflowError):
        pass
    super().__init__(f"Cloudflare answered HTTP {status_code}" + (f": {reasons}" if reasons else "")
                     + (f" (retry after {self.retry_after_seconds}s)" if self.retry_after_seconds is not None else ""))


class Cloudflare(Base):
  """Cloudflare is part of the platform: its token and domain are required env vars, so
  changing them is a restart. Calls the REST API directly over httpx."""

  def __init__(self):
    super().__init__()
    self.config = {
        "api_token": get_env("CLOUDFLARE_API_TOKEN"),
        "domain": get_env("DOMAIN"),
    }
    self.domain = self.config["domain"]
    # The manager's only address; its DNS-only record points at the tailnet IP.
    self.manager_hostname = f"{get_env('TS_NAME')}.core.{self.domain}"
    self.http = httpx.Client(base_url=config.CLOUDFLARE_API_URL, timeout=15)
    self.zone_id = None
    # The account holding the zone, found with it.
    self.account_id = None
    self.tunnel_id = None
    self.tunnel_name = f"sage-{self.domain}"
    # Each tunnel's health (healthy, degraded, down or inactive) by name, the platform's and the workers', as
    # `tunnels` last listed them.
    self.tunnel_health: dict[str, str] = {}

  @property
  def tunnel_status(self) -> str | None:
    """The platform tunnel's health as last listed."""
    return self.tunnel_health.get(self.tunnel_name)

  def is_manager_hostname(self, hostname: str | None) -> bool:
    return (hostname or "").lower() == self.manager_hostname.lower()

  def request(self, method: str, path: str, token: str | None = None, **kwargs) -> dict:
    """One API call; returns the response body. Retries network errors, rate limits and
    server errors; raises CloudflareError when the API refuses the request."""
    headers = {"Authorization": f"Bearer {token or self.config['api_token']}"}

    def attempt():
      response = self.http.request(method, path, headers=headers, **kwargs)
      if response.is_success:
        return response.json()
      try:
        errors = response.json().get("errors") or []
      except ValueError:
        errors = []
      raise CloudflareError(response.status_code, "; ".join(e.get("message", "") for e in errors), response.headers.get("Retry-After"))

    return retry(attempt, delays=config.CLOUDFLARE_RETRY_DELAYS)

  def list_all(self, path: str, per_page: int = 100, **params) -> list[dict]:
    """Every item of a paginated list."""
    items, page = [], 1
    while True:
      body = self.request("GET", path, params={**params, "page": page, "per_page": per_page})
      items.extend(body["result"])
      if page >= (body.get("result_info") or {}).get("total_pages", 1):
        return items
      page += 1

  def find_zone(self, domain: str, token: str | None = None) -> dict:
    """The active zone of `domain` that the token can see. A domain added to several accounts
    is active in only one of them. Raises with the reason when there is no such zone, or more
    than one."""
    zones = self.request("GET", "/zones", token=token, params={"name": domain, "status": "active"})["result"]
    if not zones:
      raise ValueError(
          f"No active zone for {domain} is visible to this API token. Check that the domain's "
          "nameservers are Cloudflare's.")
    if len(zones) > 1:
      raise ValueError(f"This API token sees {len(zones)} active zones for {domain}; it must see only one.")
    if not (zones[0].get("account") or {}).get("id"):
      raise ValueError(f"Cloudflare did not say which account holds the zone for {domain}.")
    return zones[0]

  def connect(self):
    """Verify the token, find the zone and its account, and make sure the platform's tunnel and
    the manager's DNS record exist. Runs at startup and again on use if that failed; raises when
    the API fails, leaving it unconnected so the next use retries."""
    with self.lock:
      if self.zone_id is not None:
        return

      if self.request("GET", "/user/tokens/verify")["result"]["status"] != "active":
        raise ValueError("The Cloudflare API token is not active.")
      zone = self.find_zone(self.domain)

      # Set first: the record calls below go through connect() too.
      self.zone_id = zone["id"]
      self.account_id = zone["account"]["id"]
      try:
        # The platform's tunnel, which no worker's removal deletes.
        self.tunnel_id = self.create_tunnel(self.tunnel_name)
        self.create_dns_record(
            name=self.manager_hostname,
            content=Tailscale().ip(),
            comment="sage-manager",
            type="A",
        )
      except Exception:
        self.zone_id = None
        self.account_id = None
        raise

      logger.info(f"Cloudflare connected for domain: {self.domain} with zone ID: {self.zone_id}")

  def check_config(self, values: dict):
    """Check a Cloudflare config against the API; raises with the reason. Every field is
    mandatory."""
    missing = [field for field in ("api_token", "domain") if not values.get(field)]
    if missing:
      raise ValueError(f"Missing values for: {', '.join(missing)}.")

    token = values["api_token"]
    if self.request("GET", "/user/tokens/verify", token=token)["result"]["status"] != "active":
      raise ValueError("The API token is not active.")
    zone = self.find_zone(values["domain"], token)
    # Tunnel access for the zone's account
    self.request("GET", f"/accounts/{zone['account']['id']}/cfd_tunnel", token=token, params={"per_page": 1})

  def get_dns_records(self, name: str | None = None) -> list[dict]:
    """The zone's records at `name`, or all of them in one call (a page can hold 5,000,000)."""
    self.connect()
    if name is None:
      return self.list_all(f"/zones/{self.zone_id}/dns_records", per_page=5000)
    return [record for record in self.list_all(f"/zones/{self.zone_id}/dns_records", name=name) if record["name"] == name]

  def sync_dns_records(self, records: dict[str, dict[str, str]], zone: list[dict] | None = None, **fields):
    """Make the zone's records with each comment exactly its set in `records` (comment to name to content), each with
    `fields` (type, proxied), from one list of the zone (`zone`, when it was listed already): those at another name
    are deleted, the others created or corrected."""
    if zone is None:
      zone = self.get_dns_records()
    for comment, wanted in records.items():
      # Cloudflare matches a comment in any case, so the comparison is here.
      existing = [record for record in zone if record.get("comment") == comment]
      for record in existing:
        if record["name"] not in wanted:
          self.request("DELETE", f"/zones/{self.zone_id}/dns_records/{record['id']}")
          logger.info(f"DNS record for {record['name']} ({comment}) deleted.")
      for name, content in wanted.items():
        self.create_dns_record(name, content, comment=comment, existing=existing, **fields)

  def sync_worker_records(self, hostname: str, subdomain: str | None = None, ip: str | None = None):
    """Point the worker's `*.<subdomain>.int.<domain>` at its IP and delete its records at any other name; without a
    subdomain, delete them all."""
    self.sync_dns_records(
        {f"sage-worker-{hostname}": {f"*.{subdomain}.int.{self.domain}": ip} if subdomain else {}}, type="A")

  def delete_dns_records(self, name, type=None):
    """Delete every record at `name` (optionally filtered by type)."""
    deleted = 0
    for record in self.get_dns_records(name):
      if type is None or record["type"] == type:
        self.request("DELETE", f"/zones/{self.zone_id}/dns_records/{record['id']}")
        deleted += 1
    if deleted:
      logger.info(f"Deleted {deleted} record(s) at {name}.")

  def create_dns_record(self, name, content, comment=None, existing=None, **fields):
    """
    Create a DNS record if it doesn't already exist, or correct its requested fields. Existence is
    by name and, when given, comment, among `existing` when given, else the records at `name`.
    `fields` are other record fields (type, proxied, ttl).
    """
    # Only DNS-only A records get an explicit TTL
    if fields.get("type") == "A":
      fields.setdefault("ttl", 60)
    record = {"name": name, "content": content, **fields}
    if comment is not None:
      record["comment"] = comment

    existing_record = next(
        (r for r in (self.get_dns_records(name) if existing is None else existing)
         if r["name"] == name and (comment is None or r.get("comment") == comment)), None)

    if existing_record:
      if not dns_record_matches(existing_record, record):
        self.request("PUT", f"/zones/{self.zone_id}/dns_records/{existing_record['id']}", json=record)
        logger.info(f"DNS record for {name} updated.")
      else:
        logger.info(f"DNS record for {name} already exists.")
    else:
      self.request("POST", f"/zones/{self.zone_id}/dns_records", json=record)
      logger.info(f"DNS record for {name} created with content {content}.")

  def worker_tunnel_name(self, hostname: str) -> str:
    return f"sage-{self.domain}-{hostname}"

  def find_tunnel(self, name) -> dict | None:
    tunnels = self.list_all(f"/accounts/{self.account_id}/cfd_tunnel", name=name, is_deleted="false")
    return next((t for t in tunnels if t["name"] == name), None)

  def tunnels(self) -> dict[str, dict]:
    """The account's tunnels by name, each with its `id` and health (`status`: healthy, degraded, down or inactive), in
    one call (a page holds up to 1,000); their health is kept as `tunnel_health`."""
    self.connect()
    tunnels = {tunnel["name"]: tunnel
               for tunnel in self.list_all(f"/accounts/{self.account_id}/cfd_tunnel", per_page=1000, is_deleted="false")}
    self.tunnel_health = {name: tunnel.get("status") for name, tunnel in tunnels.items()}
    return tunnels

  def create_tunnel(self, name):
    """The id of the tunnel of that name, created when missing, with the static ingress every tunnel carries."""
    tunnel = self.find_tunnel(name)

    if tunnel:
      logger.info(f"Tunnel {name} already exists with id: {tunnel['id']}.")
    else:
      tunnel = self.request(
          "POST", f"/accounts/{self.account_id}/cfd_tunnel",
          json={"name": name, "config_src": "cloudflare"})["result"]
      logger.info(f"Tunnel {name} created successfully with id: {tunnel['id']}.")

    # Push ingress rules to Cloudflare
    self.request(
        "PUT", f"/accounts/{self.account_id}/cfd_tunnel/{tunnel['id']}/configurations",
        json={"config": {"ingress": [
            {"hostname": f"*.{self.domain}", "service": "http://traefik:80"},
            {"service": "http_status:404"},
        ]}})
    return tunnel["id"]

  def delete_tunnel(self, name):
    """Delete the tunnel of that name, if there is one: its connections first, as a tunnel with any can't be."""
    self.connect()
    tunnel = self.find_tunnel(name)
    if tunnel is None:
      logger.info(f"No tunnel {name} to delete.")
      return
    self.request("DELETE", f"/accounts/{self.account_id}/cfd_tunnel/{tunnel['id']}/connections")
    self.request("DELETE", f"/accounts/{self.account_id}/cfd_tunnel/{tunnel['id']}")
    logger.info(f"Tunnel {name} deleted.")

  def get_tunnel_token(self, tunnel_id=None):
    self.connect()
    if tunnel_id is None:
      tunnel_id = self.tunnel_id
    token = self.request("GET", f"/accounts/{self.account_id}/cfd_tunnel/{tunnel_id}/token")["result"]
    return dict_to_obj({"id": tunnel_id, "token": token})
