import subprocess
from pathlib import Path


class LocalWorkers:
  """Workers whose Sage home is a folder each under `root`; their commands run in a local shell, and every file
  written or command run is logged in order."""

  def __init__(self, root):
    self.root = root
    self.log = []

  def home(self, hostname):
    return self.root / hostname

  def files(self, hostname):
    dynamic = self.home(hostname) / "traefik/dynamic"
    return {path.name: path.read_text() for path in dynamic.iterdir()} if dynamic.exists() else {}

  def place(self, hostname, name, content="x: 1\n"):
    dynamic = self.home(hostname) / "traefik/dynamic"
    dynamic.mkdir(parents=True, exist_ok=True)
    (dynamic / name).write_text(content)

  async def sync_file(self, hostname, src, dst, keys=None, formatter=None, timeout=120, mode=0o644):
    content = Path(src).read_text()
    for key, value in (keys or {}).items():
      content = content.replace("${" + key + "}", str(value))
    path = self.home(hostname) / Path(dst).relative_to("/opt/sage")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(mode)
    self.log.append((hostname, "write", dst))

  async def exec_command(self, hostname, command, timeout=300):
    self.home(hostname).mkdir(parents=True, exist_ok=True)
    result = subprocess.run(command.replace("/opt/sage", str(self.home(hostname))), shell=True, capture_output=True, text=True)
    self.log.append((hostname, "run", command))
    return result.returncode, result.stdout.splitlines()
