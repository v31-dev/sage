import asyncio
import json
import logging
import os
import posixpath
import subprocess

import asyncssh

from services.base import Base
from utils.common import dict_to_obj

logger = logging.getLogger(__name__)

SSH_USER = "root"


class Tailscale(Base):
  def __init__(self):
    super().__init__()

    self.uds = "/var/run/tailscale/tailscaled.sock"
    # validate connection on startup
    self.ip()
    logger.info("Connected to Tailscale.")

  def status(self):
    try:
      result = subprocess.run(
          ["tailscale", "status", "--json"],
          capture_output=True, text=True, timeout=10,
      )
    except subprocess.TimeoutExpired:
      raise Exception("Tailscale status timed out after 10s (tailscaled unresponsive?).")
    if result.returncode != 0:
      raise Exception(f"Tailscale status failed: {result.stderr.strip()}")
    return json.loads(result.stdout)

  def ip(self):
    if not hasattr(self, "_ip"):
      try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=10,
        )
      except subprocess.TimeoutExpired:
        raise Exception("Tailscale ip timed out after 10s (tailscaled unresponsive?).")
      self._ip = str(result.stdout).strip()
      if self._ip == "":
        raise Exception("Failed to get Tailscale IP address.")
      logger.info(f"Tailscale IP address: {self._ip}")
    return self._ip

  def get_by_tag(self, tag):
    """
    Get a list of objects with hostname, ip, online attributes for all peers with a given tag.
    """
    status = self.status()

    return [
        dict_to_obj(
            {
                "hostname": peer["HostName"],
                "ip": peer["TailscaleIPs"][0],
                "online": peer["Online"],
            }
        )
        for peer in status.get("Peer", {}).values()
        if any(_tag == f"tag:{tag}" for _tag in peer.get("Tags", []))
    ]

  async def exec_command(self, hostname, command, timeout=300):
    """
    Execute a command on a worker over the Tailscale SSH server via asyncssh.
    Returns (exit_status, output_lines); raises on timeout or non-zero exit.
    """
    logger.info(f"Executing command on {hostname}: {command}")
    try:
      async with asyncssh.connect(
          hostname, username=SSH_USER, known_hosts=None, connect_timeout=30
      ) as conn:
        # merge stderr into stdout so a failed command's output is captured
        result = await conn.run(
            command, timeout=timeout, check=False, stderr=asyncssh.STDOUT)
    except (asyncio.TimeoutError, asyncssh.TimeoutError):
      raise Exception(f"Command on {hostname} timed out after {timeout}s: {command}")
    except (OSError, asyncssh.Error) as e:
      raise Exception(f"Error executing command on {hostname}: {e}")

    output_lines = [
        line for line in (result.stdout or "").splitlines()
        if "Warning: client version" not in line
    ]
    for line in output_lines:
      logger.info(f"[{hostname}] {line}")

    if result.exit_status != 0:
      tail = "\n".join(output_lines[-10:])
      raise Exception(
          f"Error executing command on {hostname}: exit code {result.exit_status}"
          + (f"\n{tail}" if tail else ""))

    logger.info(f"Successfully executed command on {hostname}.")
    return (result.exit_status, output_lines)

  async def sync_file(self, hostname, src, dst, keys=None, timeout=120):
    """
    Copy a single file to a worker via SFTP over the Tailscale SSH server.
    If keys (dict) is given, src is treated as a template and ${VAR} placeholders
    are substituted before upload. Missing parent directories are created.

    `timeout` caps the whole connect+transfer so a wedged sync can't hold a
    task's scope forever.
    """
    if os.path.isdir(src):
      raise Exception(f"src must be a file, not a directory: {src}")

    content = None
    if keys:
      with open(src, "r") as f:
        content = f.read()
      for key, value in keys.items():
        content = content.replace("${" + key + "}", str(value))

    remote_dir = posixpath.dirname(dst)

    async def _upload():
      async with asyncssh.connect(
          hostname, username=SSH_USER, known_hosts=None, connect_timeout=30
      ) as conn:
        async with conn.start_sftp_client() as sftp:
          if remote_dir:
            await sftp.makedirs(remote_dir, exist_ok=True)
          if content is not None:
            async with sftp.open(dst, "w") as remote_file:
              await remote_file.write(content)
          else:
            await sftp.put(str(src), dst)

    logger.info(f"Syncing file to {hostname} from {src} to {dst}.")
    try:
      await asyncio.wait_for(_upload(), timeout)
    except asyncio.TimeoutError:
      raise Exception(f"Syncing file to {hostname} timed out after {timeout}s: {dst}")
    except (OSError, asyncssh.Error) as e:
      raise Exception(f"Error syncing file to {hostname}: {e}")
    logger.info(f"Successfully synced file to {hostname}: {dst}.")
