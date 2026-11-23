import re
import shutil
import subprocess
from dataclasses import fields, replace
from pathlib import Path

import pytest
import yaml

import config
from services.tailscale import Tailscale
from services.workers.rendering import (DEFAULT_DNS_CONTAINER, FIREWALL_FILES, INFRA_TEMPLATES, PRIVATE_RANGES, InfraInputs, firewall_fingerprint,
                                        infra_fingerprint, render_firewall, render_infrastructure, stack_scope, worker_scope)
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


def nft_runs():
  """Whether `nft` can load a table here, in a network namespace of its own that needs no privilege."""
  return shutil.which("nft") is not None and shutil.which("unshare") is not None and subprocess.run(
      ["unshare", "-rn", "nft", "list", "ruleset"], capture_output=True).returncode == 0


def firewall(resolvers=()):
  return {file.path: file for file in render_firewall(resolvers, "/opt/sage")}


def set_elements(table, name):
  body = re.search(r"set " + name + r" \{(.*?)\n  \}", table, re.S).group(1)
  found = re.search(r"elements = \{(.*?)\}", body)
  return [element.strip() for element in found.group(1).split(",")] if found else []


class TestFirewall:
  def test_it_is_the_table_and_the_unit_that_loads_it_from_the_home_and_removes_it_when_stopped(self):
    files = firewall()
    assert list(files) == list(FIREWALL_FILES) and {file.mode for file in files.values()} == {0o644}
    unit = files["firewall/sage-firewall.service"].content
    assert "ExecStart=nft -f /opt/sage/firewall/sage.nft" in unit and "ExecStop=-nft delete table inet sage" in unit

  def test_each_range_and_resolver_goes_to_the_set_of_its_version(self):
    table = firewall(("10.0.0.2", "fd00::53", "192.168.0.1"))["firewall/sage.nft"].content
    assert set_elements(table, "resolvers4") == ["10.0.0.2", "192.168.0.1"]
    assert set_elements(table, "resolvers6") == ["fd00::53"]
    assert "100.64.0.0/10" in set_elements(table, "private4") and "fc00::/7" in set_elements(table, "private6")
    assert sorted(set_elements(table, "private4") + set_elements(table, "private6")) == sorted(PRIVATE_RANGES)

  def test_without_resolvers_their_sets_are_empty(self):
    table = firewall()["firewall/sage.nft"].content
    assert set_elements(table, "resolvers4") == set_elements(table, "resolvers6") == []

  def test_no_placeholder_is_left(self):
    for path, file in firewall(("10.0.0.2",)).items():
      assert re.findall(r"\$\{\w+\}", file.content) == [], path

  @pytest.mark.skipif(not nft_runs(), reason="nft can't run in a network namespace of its own here")
  @pytest.mark.parametrize("resolvers", [(), ("10.0.0.2", "fd00::53")], ids=["no-resolvers", "resolvers"])
  def test_nft_loads_the_table_and_loads_it_again_over_itself(self, resolvers, tmp_path):
    path = tmp_path / "sage.nft"
    path.write_text(firewall(resolvers)["firewall/sage.nft"].content)
    loaded = subprocess.run(["unshare", "-rn", "sh", "-c", f"nft -f {path} && nft list table inet sage && echo --- "
                             f"&& nft -f {path} && nft list table inet sage"], capture_output=True, text=True)
    assert loaded.returncode == 0, loaded.stderr
    once, twice = loaded.stdout.split("---\n")
    assert once == twice and once.count("hook forward") == 1 and once.count("hook input") == 1

  def test_the_container_setup_reads_dockers_resolvers_from_has_none_of_its_own(self):
    services = yaml.safe_load((config.TEMPLATES_DIR / "worker/docker-compose.yml").read_text())["services"]
    (service,) = [service for service in services.values() if service.get("container_name") == DEFAULT_DNS_CONTAINER]
    assert "dns" not in service and "network_mode" not in service

  def test_the_fingerprint_follows_the_resolvers(self):
    assert firewall_fingerprint(render_firewall((), "/opt/sage")) == firewall_fingerprint(render_firewall((), "/opt/sage"))
    assert firewall_fingerprint(render_firewall(("10.0.0.2",), "/opt/sage")) != firewall_fingerprint(render_firewall((), "/opt/sage"))

  @pytest.mark.parametrize("name", FIREWALL_FILES)
  def test_a_changed_template_changes_the_fingerprint(self, name, tmp_path, monkeypatch):
    before = firewall_fingerprint(render_firewall((), "/opt/sage"))
    shutil.copytree(config.TEMPLATES_DIR, tmp_path / "templates")
    monkeypatch.setattr(config, "TEMPLATES_DIR", tmp_path / "templates")
    with open(tmp_path / "templates/worker" / name, "a") as template:
      template.write("\n# changed\n")
    assert firewall_fingerprint(render_firewall((), "/opt/sage")) != before


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
