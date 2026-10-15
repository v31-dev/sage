import logging
import time

import httpx

from services.base import Base
from services.tailscale import Tailscale
from utils.common import dict_to_obj, get_env

logger = logging.getLogger(__name__)

API_URL = "https://api.cloudflare.com/client/v4"
# Network errors, rate limits and server errors are retried after these delays (seconds).
RETRY_DELAYS = (1, 2)


class CloudflareError(Exception):
  """The Cloudflare API refused a request; carries the API's own error messages."""

  def __init__(self, status_code: int, reasons: str):
    super().__init__(f"Cloudflare answered HTTP {status_code}" + (f": {reasons}" if reasons else ""))
    self.status_code = status_code
    self.transient = status_code >= 500 or status_code == 429


class Cloudflare(Base):
  """Cloudflare is part of the platform: its token, account and domain are required env
  vars, so changing them is a restart. Calls the REST API directly over httpx."""

  def __init__(self):
    super().__init__()
    self.config = {
        "api_token": get_env("CLOUDFLARE_API_TOKEN"),
        "account_id": get_env("CLOUDFLARE_ACCOUNT_ID"),
        "domain": get_env("DOMAIN"),
    }
    self.domain = self.config["domain"]
    self.account_id = self.config["account_id"]
    # The manager's only address; its DNS-only record points at the tailnet IP.
    self.manager_hostname = f"{get_env('TS_NAME')}.core.{self.domain}"
    self.http = httpx.Client(base_url=API_URL, timeout=15)
    self.zone_id = None
    self.tunnel_id = None
    # healthy, degraded, down or inactive; refreshed by the periodic settings check.
    self.tunnel_status = None

  def request(self, method: str, path: str, token: str | None = None, **kwargs) -> dict:
    """One API call; returns the response body. Retries network errors, rate limits and
    server errors; raises CloudflareError when the API refuses the request."""
    headers = {"Authorization": f"Bearer {token or self.config['api_token']}"}
    for delay in (*RETRY_DELAYS, None):
      try:
        response = self.http.request(method, path, headers=headers, **kwargs)
      except httpx.TransportError:
        if delay is None:
          raise
        time.sleep(delay)
        continue
      if response.is_success:
        return response.json()
      try:
        errors = response.json().get("errors") or []
      except ValueError:
        errors = []
      error = CloudflareError(response.status_code, "; ".join(e.get("message", "") for e in errors))
      if not error.transient or delay is None:
        raise error
      time.sleep(delay)

  def list_all(self, path: str, **params) -> list[dict]:
    """Every item of a paginated list."""
    items, page = [], 1
    while True:
      body = self.request("GET", path, params={**params, "page": page, "per_page": 100})
      items.extend(body["result"])
      if page >= (body.get("result_info") or {}).get("total_pages", 1):
        return items
      page += 1

  def connect(self):
    """Verify the token, find the zone, and make sure the tunnel and the manager's DNS
    record exist. Runs at startup and again on use if that failed; raises when the API
    fails, leaving it unconnected so the next use retries."""
    with self.lock:
      if self.zone_id is not None:
        return

      if self.request("GET", "/user/tokens/verify")["result"]["status"] != "active":
        raise ValueError("The Cloudflare API token is not active.")
      zones = self.request("GET", "/zones", params={"name": self.domain})["result"]
      if not zones:
        raise ValueError(f"No Cloudflare zone for {self.domain} is visible to this API token.")

      # Set first: the record calls below go through connect() too.
      self.zone_id = zones[0]["id"]
      try:
        self.tunnel_id = self.create_tunnel(f"{self.domain}-sage")
        self.create_dns_record(
            name=self.manager_hostname,
            content=Tailscale().ip(),
            comment="sage-manager",
            type="A",
        )
      except Exception:
        self.zone_id = None
        raise

      logger.info(f"Cloudflare connected for domain: {self.domain} with zone ID: {self.zone_id}")

  def check_config(self, config: dict):
    """Check a Cloudflare config against the API; raises with the reason. Every field is
    mandatory."""
    missing = [field for field in ("api_token", "account_id", "domain") if not config.get(field)]
    if missing:
      raise ValueError(f"Missing values for: {', '.join(missing)}.")

    token = config["api_token"]
    if self.request("GET", "/user/tokens/verify", token=token)["result"]["status"] != "active":
      raise ValueError("The API token is not active.")
    if not self.request("GET", "/zones", token=token, params={"name": config["domain"]})["result"]:
      raise ValueError(f"No zone for {config['domain']} is visible to this API token.")
    # Tunnel access for the account
    self.request("GET", f"/accounts/{config['account_id']}/cfd_tunnel", token=token, params={"per_page": 1})

  def get_dns_records(self, name: str) -> list[dict]:
    self.connect()
    records = self.list_all(f"/zones/{self.zone_id}/dns_records", name=name)
    return [record for record in records if record["name"] == name]

  def delete_dns_record(self, name, content):
    record = next((r for r in self.get_dns_records(name) if r["content"] == content), None)

    if record:
      self.request("DELETE", f"/zones/{self.zone_id}/dns_records/{record['id']}")
      logger.info(f"DNS record for {name} with content {content} deleted.")
    else:
      logger.info(f"No DNS record found for {name} with content {content} to delete.")

  def delete_dns_records(self, name, type=None):
    """Delete every record at `name` (optionally filtered by type)."""
    deleted = 0
    for record in self.get_dns_records(name):
      if type is None or record["type"] == type:
        self.request("DELETE", f"/zones/{self.zone_id}/dns_records/{record['id']}")
        deleted += 1
    if deleted:
      logger.info(f"Deleted {deleted} record(s) at {name}.")

  def create_dns_record(self, name, content, comment=None, **fields):
    """
    Create a DNS record if it doesn't already exist, or correct its content. Existence is
    by name and, when given, comment. `fields` are other record fields (type, proxied, ttl).
    """
    # Only DNS-only A records get an explicit TTL
    if fields.get("type") == "A":
      fields.setdefault("ttl", 60)
    record = {"name": name, "content": content, **fields}
    if comment is not None:
      record["comment"] = comment

    existing_record = next(
        (r for r in self.get_dns_records(name) if comment is None or r.get("comment") == comment), None)

    if existing_record:
      if existing_record["content"] != content:
        logger.info(
            f"DNS record for {name} exists but content is {existing_record['content']}. "
            f"Updating content to {content}.")
        self.request("PUT", f"/zones/{self.zone_id}/dns_records/{existing_record['id']}", json=record)
        logger.info(f"DNS record for {name} updated with new content {content}.")
      else:
        logger.info(f"DNS record for {name} already exists.")
    else:
      self.request("POST", f"/zones/{self.zone_id}/dns_records", json=record)
      logger.info(f"DNS record for {name} created with content {content}.")

  def create_tunnel(self, name):
    tunnels = self.list_all(f"/accounts/{self.account_id}/cfd_tunnel", name=name, is_deleted="false")
    tunnel = next((t for t in tunnels if t["name"] == name), None)

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

    # Create DNS record for the tunnel
    self.create_dns_record(
        name=f"*.{self.domain}",
        content=f"{tunnel['id']}.cfargotunnel.com",
        comment=tunnel["name"],
        type="CNAME",
        proxied=True,
    )
    return tunnel["id"]

  def get_tunnel_token(self, tunnel_id=None):
    self.connect()
    if tunnel_id is None:
      tunnel_id = self.tunnel_id
    token = self.request("GET", f"/accounts/{self.account_id}/cfd_tunnel/{tunnel_id}/token")["result"]
    return dict_to_obj({"id": tunnel_id, "token": token})
