import re
import tomllib
from pathlib import Path

import pytest
import yaml

import config

TEMPLATES = Path(__file__).parent.parent.parent / "templates" / "worker"


def telegraf_config():
  """The template with every ${PLACEHOLDER} filled by a stand-in, as the manager fills them."""
  text = (TEMPLATES / "telegraf" / "telegraf.conf").read_text()
  return tomllib.loads(re.sub(r"\$\{(\w+)\}", lambda m: {"INTERVAL": str(config.REPORT_INTERVAL_SECONDS)}.get(m[1], "x"), text))


def service(name):
  return yaml.safe_load((TEMPLATES / "docker-compose.yml").read_text())["services"][name]


def telegraf_service():
  return service("telegraf")


class TestTelegrafConfig:
  def test_it_is_toml_with_the_inputs_the_manager_reads(self):
    assert set(telegraf_config()["inputs"]) == {"cpu", "mem", "system", "disk", "net", "docker", "net_response"}

  def test_it_gathers_and_flushes_at_the_report_interval(self):
    agent = telegraf_config()["agent"]
    assert agent["interval"] == agent["flush_interval"] == f"{config.REPORT_INTERVAL_SECONDS}s"

  def test_it_pushes_gzip_json_to_the_report_route_with_the_workers_name(self):
    output, = telegraf_config()["outputs"]["http"]
    assert output["url"].endswith(":9001/api/telegraf/report")
    assert output["data_format"] == "json" and output["content_encoding"] == "gzip"
    assert output["headers"]["X-Node-Name"] == "x"

  def test_one_report_holds_a_whole_gather(self):
    agent = telegraf_config()["agent"]
    assert agent["metric_batch_size"] == agent["metric_buffer_limit"]

  def test_it_measures_the_filesystem_of_the_directory_it_has_mounted(self):
    (disk,) = telegraf_config()["inputs"]["disk"]
    mounts = [v.split(":")[1] for v in telegraf_service()["volumes"] if v.endswith("/telegraf:/etc/telegraf:ro")]
    assert disk["mount_points"] == ["/etc/telegraf"] and mounts == ["/etc/telegraf"]

  def test_the_container_label_the_manager_compares_comes_through_as_a_tag(self):
    (docker,) = telegraf_config()["inputs"]["docker"]
    assert docker["docker_label_include"] == ["sage.deployed_at"]


class TestTelegrafService:
  def test_it_has_the_least_privilege_the_probe_found_enough(self):
    service = telegraf_service()
    assert service["cap_drop"] == ["ALL"]
    assert service["read_only"] is True
    assert "no-new-privileges:true" in service["security_opt"]
    assert not service.get("privileged") and "pid" not in service

  def test_the_socket_and_the_configuration_are_read_only(self):
    volumes = telegraf_service()["volumes"]
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in volumes
    assert all(v.endswith(":ro") for v in volumes)

  def test_it_runs_as_a_user_in_the_sockets_group_on_the_host_network(self):
    service = telegraf_service()
    assert service["user"] == "telegraf:${DOCKER_GID}"
    assert service["network_mode"] == "host"


class TestMemoryLimits:
  """Their executables are large and the kernel keeps the pages it has read in the container's memory, which on a
  small worker is memory the applications could use."""

  @pytest.mark.parametrize("name", ["telegraf", "vector"])
  def test_the_log_and_metric_collectors_have_a_memory_limit_that_swap_does_not_extend(self, name):
    limits = service(name)
    assert limits["mem_limit"].endswith("m")
    assert limits["memswap_limit"] == limits["mem_limit"]
