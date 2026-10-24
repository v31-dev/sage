import asyncio
import contextlib
import json
import logging
import os
import posixpath
import secrets
import shlex
import subprocess
import time
from pathlib import Path

import asyncssh

import config
from services.base import Base
from utils.common import dict_to_obj, get_env

logger = logging.getLogger(__name__)

SSH_USER = "root"


class Tailscale(Base):
  def __init__(self):
    super().__init__()

    self.uds = "/var/run/tailscale/tailscaled.sock"
    self.worker_tag = get_env("TS_WORKER_TAG").strip().removeprefix("tag:")
    # Pooled SSH connections per worker
    self._conns: dict[str, asyncssh.SSHClientConnection] = {}
    self._conn_locks: dict[str, asyncio.Lock] = {}

  async def _get_conn(self, hostname):
    """Return a live pooled SSH connection to `hostname`, connecting if needed."""
    conn = self._conns.get(hostname)
    if conn is not None and not conn.is_closed():
      return conn
    lock = self._conn_locks.setdefault(hostname, asyncio.Lock())
    async with lock:
      conn = self._conns.get(hostname)
      if conn is not None and not conn.is_closed():
        return conn
      # keepalive: ping every 30s, drop after ~90s of silence, so a silently
      # dead pooled connection is detected (is_closed) and reconnected instead
      # of hanging the next command, and idle connections stay NAT/firewall-warm.
      # tailscaled runs in userspace mode, so there is no route into the tailnet;
      # `tailscale nc` carries the connection through tailscaled instead.
      conn = await asyncssh.connect(
          hostname, username=SSH_USER, known_hosts=None, connect_timeout=30,
          keepalive_interval=30, keepalive_count_max=3,
          proxy_command=["tailscale", "nc", hostname, "22"])
      self._conns[hostname] = conn
      return conn

  async def _evict(self, hostname):
    """Drop and close a pooled connection (after an error, or a removed worker)."""
    conn = self._conns.pop(hostname, None)
    if conn is not None:
      conn.close()

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
    if not getattr(self, "_ip", ""):
      try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True, text=True, timeout=10,
        )
      except subprocess.TimeoutExpired:
        raise Exception("Tailscale ip timed out after 10s (tailscaled unresponsive?).")
      ip = str(result.stdout).strip()
      if ip == "":
        raise Exception("Failed to get Tailscale IP address.")
      self._ip = ip
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

  async def _start_command(self, hostname, command):
    """Open a channel on a pooled connection and start `command` on it. A stale pooled
    connection is evicted and the open retried once on a fresh one; that is safe because
    nothing has run yet. Once this returns the command is running and is never retried."""
    for attempt in range(2):
      try:
        conn = await self._get_conn(hostname)
      except (OSError, asyncssh.Error) as e:
        raise Exception(f"Error connecting to {hostname}: {e}")
      try:
        # stderr is merged into stdout so a failed command's output is captured
        return await conn.create_process(command, stderr=asyncssh.STDOUT, errors="replace")
      except (OSError, asyncssh.Error) as e:
        await self._evict(hostname)
        if attempt == 1:
          raise Exception(f"Error executing command on {hostname}: {e}")

  async def exec_command(self, hostname, command, timeout=300):
    """
    Execute a command on a worker over the Tailscale SSH server via asyncssh,
    reusing a pooled connection. Each output line is logged as it arrives. Returns
    (exit_status, output_lines); raises on timeout or non-zero exit.

    The worker stops the command at `timeout` (GNU or uutils `timeout`, run through the
    session's own shell): Tailscale SSH ignores signals and a closed channel, so nothing the
    manager does afterwards would stop it. The manager also gives up on a worker that answers
    nothing, a little after the deadline.
    """
    if timeout <= 0:
      raise ValueError(f"timeout must be positive, got {timeout}")  # `timeout 0` would mean no deadline
    logger.info(f"Executing command on {hostname}: {command}")
    wrapped = (
        f'timeout -k {config.COMMAND_KILL_AFTER_SECONDS} {timeout} "${{SHELL:-/bin/sh}}" -c {shlex.quote(command)}')
    process = await self._start_command(hostname, wrapped)

    output_lines = []
    started = time.monotonic()
    try:
      async with asyncio.timeout(timeout + config.COMMAND_WAIT_GRACE_SECONDS):
        async for chunk in process.stdout:
          for line in chunk.splitlines():
            if "Warning: client version" in line:
              continue
            output_lines.append(line)
            logger.info(f"[{hostname}] {line}")
        result = await process.wait()
    except TimeoutError:
      await self._evict(hostname)  # the worker is not answering; do not reuse the connection
      raise Exception(f"Command on {hostname} timed out after {timeout}s: {command}")
    except (OSError, asyncssh.Error) as e:
      await self._evict(hostname)  # connection likely dead
      raise Exception(f"Error executing command on {hostname}: {e}")
    finally:
      process.close()

    status = result.exit_status
    # `timeout` exits 124 at the deadline, or 137 when it had to kill the command
    if status in (124, 137) and time.monotonic() - started >= timeout - 1:
      raise Exception(f"Command on {hostname} timed out after {timeout}s: {command}")
    if status != 0:
      tail = "\n".join(output_lines[-10:])
      raise Exception(
          f"Error executing command on {hostname}: exit code {status}"
          + (f"\n{tail}" if tail else ""))

    logger.info(f"Successfully executed command on {hostname}.")
    return (status, output_lines)

  async def sync_file(self, hostname, src, dst, keys=None, formatter=None, timeout=120, mode=None):
    """
    Copy a single file to a worker via SFTP over the Tailscale SSH server.
    If keys (dict) is given, src is treated as a template and ${VAR} placeholders
    are substituted before upload. If formatter is given, it receives the file
    text (after any substitution) and returns the text to upload. Either one
    reads src as text; with neither, src is copied verbatim. Missing parent
    directories are created.

    Without `mode` the file is written in place with the server's default mode. With
    `mode` (secrets, scripts) the file is created with that mode, uploaded to a temporary
    name beside `dst` and renamed over it: nobody reads a partial file, and a `dst` that
    was written earlier with a looser mode is replaced rather than rewritten, because a
    rewrite keeps the old mode.

    `timeout` caps the whole connect+transfer so a wedged sync can't hold a
    task's scope forever.
    """
    if os.path.isdir(src):
      raise Exception(f"src must be a file, not a directory: {src}")

    content = None
    if keys or formatter:
      with open(src, "r") as f:
        content = f.read()
      if keys:
        for key, value in keys.items():
          content = content.replace("${" + key + "}", str(value))
      if formatter:
        content = formatter(content)

    remote_dir = posixpath.dirname(dst)

    async def _upload(conn):
      async with conn.start_sftp_client() as sftp:
        if remote_dir:
          await sftp.makedirs(remote_dir, exist_ok=True)
        if mode is not None:
          await _upload_with_mode(sftp)
        elif content is not None:
          async with sftp.open(dst, "w") as remote_file:
            await remote_file.write(content)
        else:
          await sftp.put(str(src), dst)

    async def _upload_with_mode(sftp):
      data = content.encode() if content is not None else Path(src).read_bytes()
      temporary = posixpath.join(remote_dir, f".{posixpath.basename(dst)}.{secrets.token_hex(4)}.tmp")
      try:
        async with sftp.open(temporary, "wb", asyncssh.SFTPAttrs(permissions=mode)) as remote_file:
          await remote_file.write(data)
        await sftp.posix_rename(temporary, dst)
      except BaseException:
        with contextlib.suppress(Exception):
          await sftp.remove(temporary)
        raise

    logger.info(f"Syncing file to {hostname} from {src} to {dst}.")
    for attempt in range(2):
      try:
        conn = await self._get_conn(hostname)
      except (OSError, asyncssh.Error) as e:
        raise Exception(f"Error connecting to {hostname}: {e}")
      try:
        await asyncio.wait_for(_upload(conn), timeout)
        break
      except asyncio.TimeoutError:
        raise Exception(f"Syncing file to {hostname} timed out after {timeout}s: {dst}")
      except (OSError, asyncssh.Error) as e:
        await self._evict(hostname)  # connection likely dead; retry once fresh
        if attempt == 1:
          raise Exception(f"Error syncing file to {hostname}: {e}")
    logger.info(f"Successfully synced file to {hostname}: {dst}.")
