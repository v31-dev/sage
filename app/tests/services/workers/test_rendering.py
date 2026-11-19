import re
import shutil
import subprocess
from dataclasses import fields, replace
from pathlib import Path

import pytest
import yaml

import config
from services.tailscale import Tailscale
from services.workers.rendering import INFRA_TEMPLATES, InfraInputs, infra_fingerprint, render_infrastructure, stack_scope, worker_scope
from services.workers.state import Workers
from utils.queue import _scope_conflict

INPUTS = InfraInputs(hostname="w1", address="100.64.0.2", manager_address="100.64.0.1", domain="example.test",
                     platform_tunnel="tunnel-platform", worker_tunnel="tunnel-w1", version="1.2.3", report_interval=10,
                     edge_subnets=("172.22.0.0/16", "fd00:22::/64"))


def render():
  return {file.path: file for file in render_infrastructure(INPUTS, "token-w1", "token-discovery", "998", "/opt/sage")}


class TestRendering:
  def test_the_stack_is_its_six_files_and_only_the_env_file_is_private(self):
    files = render()
    assert list(files) == ["docker-compose.yml", ".env", "traefik/traefik.yml", "traefik/dynamic/config.yml",
                           "vector/config/vector.yml", "telegraf/telegraf.conf"]
    assert {path: file.mode for path, file in files.items() if file.mode != 0o644} == {".env": 0o600}

  def test_the_compose_file_keeps_its_own_variables_for_the_env_beside_it(self):
    compose = render()["docker-compose.yml"].content
    assert compose == (config.TEMPLATES_DIR / "worker/docker-compose.yml").read_text()
    assert {"${SAGE_HOME}", "${TS_IP}", "${TUNNEL_TOKEN}", "${DOCKER_GID}"} <= set(re.findall(r"\$\{\w+\}", compose))

  def test_the_env_file_holds_what_the_compose_file_reads(self):
    assert render()[".env"].content.splitlines() == [
        "SAGE_HOME=/opt/sage", "TS_IP=100.64.0.2", "TS_HOSTNAME=w1", "TUNNEL_TOKEN=token-w1",
        "DISCOVERY_TUNNEL_TOKEN=token-discovery", "DOCKER_GID=998"]

  def test_the_collectors_report_to_the_manager_and_name_the_worker(self):
    files = render()
    assert 'uri: "http://100.64.0.1:9001/api/vector/logs"' in files["vector/config/vector.yml"].content
    telegraf = files["telegraf/telegraf.conf"].content
    assert 'url = "http://100.64.0.1:9001/api/telegraf/report"' in telegraf
    assert 'address = "100.64.0.2:443"' in telegraf and 'interval = "10s"' in telegraf
    assert 'X-Node-Name = "w1"' in telegraf

  def test_the_collectors_send_to_the_port_the_manager_listens_on(self, monkeypatch):
    monkeypatch.setattr(config, "INGEST_PORT", 9101)
    files = render()
    assert "http://100.64.0.1:9101/api/vector/logs" in files["vector/config/vector.yml"].content
    assert "http://100.64.0.1:9101/api/telegraf/report" in files["telegraf/telegraf.conf"].content

  def test_traefik_takes_forwarded_headers_from_the_edge_networks_subnets_only(self):
    static = yaml.safe_load(render()["traefik/traefik.yml"].content)
    assert static["entryPoints"]["web"]["forwardedHeaders"] == {"trustedIPs": ["172.22.0.0/16", "fd00:22::/64"]}
    assert all("forwardedHeaders" not in entry for name, entry in static["entryPoints"].items() if name != "web")

  def test_traefiks_dynamic_config_sends_no_header_of_its_own_on_an_applications_routes(self):
    middlewares = yaml.safe_load(render()["traefik/dynamic/config.yml"].content)["http"]["middlewares"]
    assert sorted(middlewares) == ["probe", "tailscale-only"]

  def test_no_placeholder_of_sages_is_left_in_a_filled_file(self):
    for path, file in render().items():
      if path != "docker-compose.yml":
        assert re.findall(r"\$\{\w+\}", file.content) == [], path


class TestFingerprint:
  def test_the_same_inputs_give_the_same_fingerprint(self):
    assert infra_fingerprint(INPUTS) == infra_fingerprint(replace(INPUTS))

  @pytest.mark.parametrize("field", [field.name for field in fields(InfraInputs)])
  def test_each_input_changes_it(self, field):
    changed = replace(INPUTS, **{field: 11 if field == "report_interval" else "other"})
    assert infra_fingerprint(changed) != infra_fingerprint(INPUTS)

  @pytest.mark.parametrize("name", INFRA_TEMPLATES)
  def test_a_changed_template_changes_it(self, name, tmp_path, monkeypatch):
    before = infra_fingerprint(INPUTS)
    shutil.copytree(config.TEMPLATES_DIR, tmp_path / "templates")
    monkeypatch.setattr(config, "TEMPLATES_DIR", tmp_path / "templates")
    with open(tmp_path / "templates/worker" / name, "a") as template:
      template.write("\n# changed\n")
    assert infra_fingerprint(INPUTS) != before


class Worker:
  """A worker whose commands run in a local shell and whose synced files land under the test's directory."""

  async def exec_command(self, hostname, command, timeout=300):
    result = subprocess.run(["sh", "-c", command], capture_output=True, text=True, check=True)
    return result.returncode, result.stdout.splitlines()

  async def sync_file(self, hostname, src, dst, keys=None, formatter=None, timeout=120, mode=0o644):
    Path(dst).parent.mkdir(parents=True, exist_ok=True)
    Path(dst).write_text(Path(src).read_text().replace("${CONTENT}", keys["CONTENT"]))


class TestRevisions:
  @pytest.fixture(autouse=True)
  def worker(self, register, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKER_HOME_DIR", str(tmp_path / "home"))
    monkeypatch.setattr(config, "WORKER_TMP_DIR", str(tmp_path / "home" / "tmp"))
    register(Tailscale, Worker())

  async def test_a_worker_without_stamps_has_none(self):
    assert await Workers().read_revisions("w1") == {}

  async def test_written_stamps_are_read_back_by_name(self):
    await Workers().write_revision("w1", "owner", "0123456789abcdef example.test")
    await Workers().write_revision("w1", "infra", "abc123")
    assert await Workers().read_revisions("w1") == {"owner": "0123456789abcdef example.test", "infra": "abc123"}

  async def test_a_stamp_is_replaced_whole(self):
    await Workers().write_revision("w1", "infra", "old")
    await Workers().write_revision("w1", "infra", "new")
    assert (await Workers().read_revisions("w1"))["infra"] == "new"


def test_a_workers_stack_is_under_the_worker_and_apart_from_another_workers():
  assert _scope_conflict(worker_scope("w1"), stack_scope("w1"))
  assert not _scope_conflict(worker_scope("w1"), stack_scope("w2"))
  assert not _scope_conflict(worker_scope("w1"), worker_scope("w10"))
