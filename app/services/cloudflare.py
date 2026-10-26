import logging

import httpx

import config
from services.base import Base
from services.tailscale import Tailscale
from utils.common import dict_to_obj, get_env
from utils.retry import retry

logger = logging.getLogger(__name__)


class CloudflareError(Exception):
  """The Cloudflare API refused a request; carries the API's own error messages."""

  def __init__(self, status_code: int, reasons: str):
    super().__init__(f"Cloudflare answered HTTP {status_code}" + (f": {reasons}" if reasons else ""))
    self.status_code = status_code
    self.transient = status_code >= 500 or status_code == 429


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
    # healthy, degraded, down or inactive; refreshed by the periodic settings check.
    self.tunnel_status = None

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
      raise CloudflareError(response.status_code, "; ".join(e.get("message", "") for e in errors))

    return retry(attempt, delays=config.CLOUDFLARE_RETRY_DELAYS)

  def list_all(self, path: str, **params) -> list[dict]:
    """Every item of a paginated list."""
    items, page = [], 1
    while True:
      body = self.request("GET", path, params={**params, "page": page, "per_page": 100})
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
    """Verify the token, find the zone and its account, and make sure the tunnel and the
    manager's DNS record exist. Runs at startup and again on use if that failed; raises when
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
        self.tunnel_id = self.create_tunnel(f"{self.domain}-sage")
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
