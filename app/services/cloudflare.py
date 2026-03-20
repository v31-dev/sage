import logging
from cloudflare import Cloudflare as CloudflareClient

from utils.common import get_env, dict_to_obj
from services.base import Base
from services.tailscale import Tailscale


logger = logging.getLogger(__name__)

class Cloudflare(Base):
  def __init__(self):
    super().__init__()
    
    self.org = get_env("ORG")
    self.domain = get_env("DOMAIN")
    self.account_id = get_env("CLOUDFLARE_ACCOUNT_ID")
    self.host_ip = Tailscale().ip()
    self.client = CloudflareClient(api_token=get_env("CLOUDFLARE_API_TOKEN"))

    # Validate the token
    if self.client.user.tokens.verify().status != "active":
      raise Exception("Invalid Cloudflare API token")

    self.zone_id = self.client.zones.list(name=self.domain).result[0].id

    # Create a tunnel
    self.tunnel_id = self.create_tunnel(f"{self.org}-sage")

    # Create a DNS record for core internal apps
    self.create_dns_record(name=f"*.core.{self.domain}", content=self.host_ip, comment=f"{self.org}-sage-manager", type="A")

    logger.info(f"Cloudflare initialized for domain: {self.domain} with zone ID: {self.zone_id}")

  def get_dns_records(self):
    return self.client.dns.records.list(zone_id=self.zone_id).result 
  
  def delete_dns_record(self, name, content):
    records = self.get_dns_records()
    record = next((r for r in records if r.name == name and r.content == content), None)

    if record:
      self.client.dns.records.delete(zone_id=self.zone_id, dns_record_id=record.id)
      logger.info(f"DNS record for {name} with content {content} deleted.")
    else:
      logger.info(f"No DNS record found for {name} with content {content} to delete.")

  def create_dns_record(self, name, content, comment=None, *args, **kwargs):
    '''
      Create a DNS record if it doesn't already exist. 
      Criteria for existence can be based on name, content and optionally comment.
    '''
    records = self.get_dns_records()

    if comment is not None:
      existing_record = next((r for r in records if r.name == name and r.comment == comment), None)
    else:  
      existing_record = next((r for r in records if r.name == name), None)
    
    if existing_record:
      # Check if the content is correct, if not update it
      if existing_record.content != content:
        logger.info(f"DNS record for {name} exists but content is {existing_record.content}. Updating content to {content}.")
        self.client.dns.records.update(zone_id=self.zone_id, dns_record_id=existing_record.id, name=name, content=content, comment=comment, *args, **kwargs)
        logger.info(f"DNS record for {name} updated with new content {content}.")
      else:
        logger.info(f"DNS record for {name} already exists.")
    else:
      self.client.dns.records.create(zone_id=self.zone_id, name=name, content=content, comment=comment, *args, **kwargs)
      logger.info(f"DNS record for {name} created with content {content}.")

  def get_tunnels(self):
    return self.client.zero_trust.tunnels.cloudflared.list(account_id=self.account_id).result

  def create_tunnel(self, name):
    # Check if tunnel already exists
    tunnels = self.get_tunnels()
    tunnel = next((t for t in tunnels if t.name == name), None)

    if tunnel:
      logger.info(f"Tunnel {name} already exists with id: {tunnel.id}.")
    else:
      tunnel = self.client.zero_trust.tunnels.cloudflared.create(account_id=self.account_id, name=name, config_src="cloudflare")
      logger.info(f"Tunnel {name} created successfully with id: {tunnel.id}.")

    # Push ingress rules to Cloudflare
    self.client.zero_trust.tunnels.cloudflared.configurations.update(
      account_id=self.account_id,
      tunnel_id=tunnel.id,
      config={
        "ingress": [
          {"hostname": f"*.{self.domain}", "service": "http://traefik:80"},
          {"service": "http_status:404"}
        ]
      }
    )

    # Create DNS record for the tunnel
    self.create_dns_record(name=f"*.{self.domain}", content=f"{tunnel.id}.cfargotunnel.com", comment=tunnel.name, type="CNAME", proxied=True)
    return tunnel.id
  
  def get_tunnel_token(self, tunnel_id = None):
    if tunnel_id is None:
      tunnel_id = self.tunnel_id
    return dict_to_obj({
      "id": tunnel_id,
      "token": self.client.zero_trust.tunnels.cloudflared.token.get(account_id=self.account_id, tunnel_id=tunnel_id)
    })