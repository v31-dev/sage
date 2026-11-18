import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import httpx

APP = Path(__file__).parent.parent

# The startup, run as `python main.py` runs it, with the setup of the domain held until the test says.
DRIVER = textwrap.dedent("""
    import asyncio, sys, threading
    from types import SimpleNamespace

    import config
    config.HEALTHCHECK_PORT = int(sys.argv[1])
    import main
    from services.base import Singleton
    from services.cloudflare import Cloudflare
    from services.manager import Manager

    def setup_domain():
      sys.stdin.readline()
      raise RuntimeError("the zone is not found")

    Singleton._instances[Manager] = SimpleNamespace(setup_domain=setup_domain)
    Singleton._instances[Cloudflare] = SimpleNamespace(manager_hostname="manager.core.example.test")
    main.initialize_encryption = lambda key: None
    asyncio.run(main.main())
""")


def free_port() -> int:
  with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    return listener.getsockname()[1]


def health(port: int) -> httpx.Response | None:
  try:
    return httpx.get(f"http://127.0.0.1:{port}/health", timeout=1)
  except httpx.TransportError:
    return None


class TestStartup:
  def test_the_health_probe_is_answered_while_the_domain_is_set_up_and_a_failed_setup_still_exits(self):
    port = free_port()
    with subprocess.Popen([sys.executable, "-c", DRIVER, str(port)], cwd=APP, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as process:
      try:
        deadline = time.monotonic() + 20
        while (response := health(port)) is None:
          assert process.poll() is None, process.stdout.read()
          assert time.monotonic() < deadline, "the health server did not start"
          time.sleep(0.1)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert process.poll() is None

        process.stdin.write("go\n")
        process.stdin.flush()
        assert process.wait(20) == 1
        output = process.stdout.read()
        assert "[CRITICAL]" in output and "Could not set up manager.core.example.test: the zone is not found" in output
        assert health(port) is None
      finally:
        process.kill()
