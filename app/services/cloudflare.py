import logging

from cloudflare import Cloudflare as CloudflareClient

from services.base import Base
from services.tailscale import Tailscale
from utils.common import dict_to_obj, get_env

logger = logging.getLogger(__name__)


class Cloudflare(Base):
  """Cloudflare is part of the platform: its token, account and domain are required env
  vars, so changing them is a restart."""

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
    self.client = None
    self.zone_id = None
    self.tunnel_id = None

  def connect(self):
    """Verify the token, find the zone, and make sure the tunnel and the manager's DNS
    record exist. Runs at startup and again on use if that failed; raises when the API
    fails, leaving it unconnected so the next use retries."""
    with self.lock:
      if self.client is not None:
        return

      client = CloudflareClient(api_token=self.config["api_token"])
      if client.user.tokens.verify().status != "active":
        raise Exception("Invalid Cloudflare API token")
      zones = client.zones.list(name=self.domain).result
      if not zones:
        raise Exception(f"Cloudflare zone not found for domain {self.domain}")

      self.client, self.zone_id = client, zones[0].id
      try:
        self.tunnel_id = self.create_tunnel(f"{self.domain}-sage")
        self.create_dns_record(
            name=self.manager_hostname,
            content=Tailscale().ip(),
            comment="sage-manager",
            type="A",
        )
      except Exception:
        self.client = None
        raise

      logger.info(f"Cloudflare connected for domain: {self.domain} with zone ID: {self.zone_id}")

  def check_config(self, config: dict):
    """Check a Cloudflare config against the API; raises with the reason. Every field is
    mandatory."""
    missing = [field for field in ("api_token", "account_id", "domain") if not config.get(field)]
    if missing:
      raise ValueError(f"Missing values for: {', '.join(missing)}.")

    client = CloudflareClient(api_token=config["api_token"], timeout=15, max_retries=1)
    if client.user.tokens.verify().status != "active":
      raise ValueError("The API token is not active.")
    if not client.zones.list(name=config["domain"]).result:
      raise ValueError(f"No zone for {config['domain']} is visible to this API token.")
    # Tunnel access for the account
    client.zero_trust.tunnels.cloudflared.list(account_id=config["account_id"])

  def get_dns_records(self):
    self.connect()
    return self.client.dns.records.list(zone_id=self.zone_id).result

  def delete_dns_record(self, name, content):
    records = self.get_dns_records()
    record = next((r for r in records if r.name == name and r.content == content), None)

    if record:
      self.client.dns.records.delete(zone_id=self.zone_id, dns_record_id=record.id)
      logger.info(f"DNS record for {name} with content {content} deleted.")
    else:
      logger.info(f"No DNS record found for {name} with content {content} to delete.")

  def delete_dns_records(self, name, type=None):
    """Delete every record at `name` (optionally filtered by type)."""
    deleted = 0
    for record in self.get_dns_records():
      if record.name == name and (type is None or record.type == type):
        self.client.dns.records.delete(zone_id=self.zone_id, dns_record_id=record.id)
        deleted += 1
    if deleted:
      logger.info(f"Deleted {deleted} record(s) at {name}.")

  def create_dns_record(self, name, content, comment=None, *args, **kwargs):
    """
    Create a DNS record if it doesn't already exist.
    Criteria for existence can be based on name, content and optionally comment.
    """
    # Only DNS-only A records get an explicit TTL
    if kwargs.get("type") == "A":
      kwargs.setdefault("ttl", 60)

    records = self.get_dns_records()

    if comment is not None:
      existing_record = next(
          (r for r in records if r.name == name and r.comment == comment), None
      )
    else:
      existing_record = next((r for r in records if r.name == name), None)

    if existing_record:
      # Check if the content is correct, if not update it
      if existing_record.content != content:
        logger.info(
            f"DNS record for {name} exists but content is {
                existing_record.content}. Updating content to {content}.")
        self.client.dns.records.update(
            zone_id=self.zone_id,
            dns_record_id=existing_record.id,
            name=name,
            content=content,
            comment=comment,
            *args,
            **kwargs,
        )
        logger.info(f"DNS record for {name} updated with new content {content}.")
      else:
        logger.info(f"DNS record for {name} already exists.")
    else:
      self.client.dns.records.create(
          zone_id=self.zone_id,
          name=name,
          content=content,
          comment=comment,
          *args,
          **kwargs,
      )
      logger.info(f"DNS record for {name} created with content {content}.")

  def get_tunnels(self):
    self.connect()
    return self.client.zero_trust.tunnels.cloudflared.list(account_id=self.account_id).result

  def create_tunnel(self, name):
    # Check if tunnel already exists
    tunnels = self.get_tunnels()
    tunnel = next((t for t in tunnels if t.name == name), None)

    if tunnel:
      logger.info(f"Tunnel {name} already exists with id: {tunnel.id}.")
    else:
      tunnel = self.client.zero_trust.tunnels.cloudflared.create(
          account_id=self.account_id, name=name, config_src="cloudflare"
      )
      logger.info(f"Tunnel {name} created successfully with id: {tunnel.id}.")

    # Push ingress rules to Cloudflare
    self.client.zero_trust.tunnels.cloudflared.configurations.update(
        account_id=self.account_id,
        tunnel_id=tunnel.id,
        config={
            "ingress": [
                {"hostname": f"*.{self.domain}", "service": "http://traefik:80"},
                {"service": "http_status:404"},
            ]
        },
    )

    # Create DNS record for the tunnel
    self.create_dns_record(
        name=f"*.{self.domain}",
        content=f"{tunnel.id}.cfargotunnel.com",
        comment=tunnel.name,
        type="CNAME",
        proxied=True,
    )
    return tunnel.id

  def get_tunnel_token(self, tunnel_id=None):
    self.connect()
    if tunnel_id is None:
      tunnel_id = self.tunnel_id
    return dict_to_obj(
        {
            "id": tunnel_id,
            "token": self.client.zero_trust.tunnels.cloudflared.token.get(
                account_id=self.account_id, tunnel_id=tunnel_id
            ),
        }
    )
