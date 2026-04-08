import json
import logging
import os
import subprocess
from tempfile import NamedTemporaryFile

from services.base import Base
from utils.common import dict_to_obj

logger = logging.getLogger(__name__)


class Tailscale(Base):
    def __init__(self):
        super().__init__()

        self.uds = "/var/run/tailscale/tailscaled.sock"
        # validate connection on startup
        self.ip()
        logger.info("Connected to Tailscale.")

    def status(self):
        result = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True)
        return json.loads(result.stdout)

    def ip(self):
        if not hasattr(self, "_ip"):
            result = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True)
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

    def sync_file(self, hostname, src, dst, keys=None):
        """
        Sync a single file using rsync over Tailscale SSH.
        If keys (dict) is provided then src will be considered as a template file and vars will
        be replaced with ${VAR} format.
        """
        if os.path.isdir(src):
            raise Exception(f"src must be a file, not a directory: {src}")

        file_to_sync = src
        temp_file = None

        if keys:
            with open(src, "r") as f:
                content = f.read()

            # Replace ${KEY} with values from keys dict
            for key, value in keys.items():
                content = content.replace("${" + key + "}", str(value))

            # Create a temporary file with substituted content
            with NamedTemporaryFile(mode="w", delete=False) as temp_file:
                temp_file.write(content)
                file_to_sync = temp_file.name

        try:
            logger.info(f"Syncing files to {hostname} from {src} to {dst}.")
            result = subprocess.run(
                [
                    "rsync",
                    "-avz",
                    "--mkpath",
                    "--no-perms",
                    "--no-owner",
                    "--no-group",
                    "-e",
                    "tailscale ssh",
                    file_to_sync,
                    f"{hostname}:{dst}",
                ],
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                raise Exception(f"Error syncing files to {hostname}: {result.stderr.strip()}")

            logger.info(f"Successfully synced files to {hostname}: {dst}.")
        finally:
            # Clean up temporary file if created
            if temp_file:
                os.remove(file_to_sync)

    def exec_command(self, hostname, command, timeout=300):
        """
        Execute a command on a remote host via Tailscale SSH.
        Streams output in real-time to logger.
        Returns (exit code, command stdout) and raises an exception if the command fails.

        Args:
          hostname: Target host to execute command on
          command: Command to execute
          timeout: Timeout in seconds (default 300s = 5min for healthchecks)
        """
        logger.info(f"Executing command on {hostname}: {command}")
        try:
            process = subprocess.Popen(
                ["tailscale", "ssh", hostname, command],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )

            # Capture output and stream line-by-line
            output_lines = []
            for line in iter(process.stdout.readline, ""):
                if line:
                    # Ignore Tailscale client warning
                    if "Warning: client version" in line:
                        continue
                    logger.info(f"[{hostname}] {line.rstrip()}")
                    output_lines.append(line.rstrip())

            process.wait(timeout=timeout)

            if process.returncode != 0:
                raise Exception(
                    f"Error executing command on {hostname}: exit code {process.returncode}"
                )

            logger.info(f"Successfully executed command on {hostname}.")
            return (process.returncode, output_lines)

        except subprocess.TimeoutExpired:
            process.kill()
            raise Exception(f"Command on {hostname} timed out after {timeout}s: {command}")
