import re
import tomllib
from pathlib import Path

import pytest
import yaml

import config
from services.containers.placement import LABEL_APPLICATION, LABEL_CONTAINER, LABELS
from services.worker_reports import LABEL_PREFIX
from services.workers.rendering import EDGE_NETWORK

TEMPLATES = Path(__file__).parent.parent.parent / "templates" / "worker"


def telegraf_config():
  """The template with every ${PLACEHOLDER} filled by a stand-in, as the manager fills them."""
  text = (TEMPLATES / "telegraf" / "telegraf.conf").read_text()
  return tomllib.loads(re.sub(r"\$\{(\w+)\}", lambda m: {"INTERVAL": str(config.REPORT_INTERVAL_SECONDS)}.get(m[1], "x"), text))


def vector_config():
  return yaml.safe_load((TEMPLATES / "vector" / "vector.yml").read_text())


def vector_sink():
  return vector_config()["sinks"]["fastapi_http"]


def service(name):
  return yaml.safe_load((TEMPLATES / "docker-compose.yml").read_text())["services"][name]


def telegraf_service():
  return service("telegraf")


class TestVectorConfig:
  def test_it_sends_the_logs_it_holds_at_least_every_ten_seconds(self):
    assert vector_sink()["batch"]["timeout_secs"] == 10

  def test_it_posts_to_the_log_route_with_the_workers_name(self):
    sink = vector_sink()
    assert sink["uri"] == "http://${IP}:${INGEST_PORT}/api/vector/logs"
    assert sink["request"]["headers"]["X-Node-Name"] == "${HOSTNAME}"

  def test_it_ships_the_containers_through_the_transform_that_picks_them(self):
    config_ = vector_config()
    assert config_["transforms"]["shipped"]["inputs"] == ["container_logs"]
    assert vector_sink()["inputs"] == ["shipped"]

  def test_it_drops_every_container_but_traefik_and_a_placements_and_names_its_application(self):
    source = vector_config()["transforms"]["shipped"]["source"]
    assert f'.container_name != "traefik" && !exists(.label."{LABEL_CONTAINER}")' in source
    assert f'"application": .label."{LABEL_APPLICATION}"' in source

  def test_its_batches_stay_well_within_what_the_intake_takes(self):
    assert vector_sink()["batch"]["max_bytes"] * 2 <= config.LOG_INTAKE_MAX_BYTES


class TestTelegrafConfig:
  def test_it_is_toml_with_the_inputs_the_manager_reads(self):
    assert set(telegraf_config()["inputs"]) == {"cpu", "mem", "system", "disk", "net", "docker", "net_response"}

  def test_it_gathers_and_flushes_at_the_report_interval(self):
    agent = telegraf_config()["agent"]
    assert agent["interval"] == agent["flush_interval"] == f"{config.REPORT_INTERVAL_SECONDS}s"

  def test_it_pushes_gzip_json_to_the_report_route_with_the_workers_name(self):
    output, = telegraf_config()["outputs"]["http"]
    assert output["url"] == "http://x:x/api/telegraf/report"
    assert output["data_format"] == "json" and output["content_encoding"] == "gzip"
    assert output["headers"]["X-Node-Name"] == "x"

  def test_one_report_holds_a_whole_gather(self):
    agent = telegraf_config()["agent"]
    assert agent["metric_batch_size"] == agent["metric_buffer_limit"]

  def test_it_measures_the_filesystem_of_the_directory_it_has_mounted(self):
    (disk,) = telegraf_config()["inputs"]["disk"]
    mounts = [v.split(":")[1] for v in telegraf_service()["volumes"] if v.endswith("/telegraf:/etc/telegraf:ro")]
    assert disk["mount_points"] == ["/etc/telegraf"] and mounts == ["/etc/telegraf"]

  def test_every_label_of_a_placement_comes_through_as_a_tag_the_reports_keep(self):
    (docker,) = telegraf_config()["inputs"]["docker"]
    assert docker["docker_label_include"] == list(LABELS)
    assert all(label.startswith(LABEL_PREFIX) for label in LABELS)


class TestApplicationHeaders:
  @pytest.mark.parametrize("name", ["service_public.yml", "service_internal.yml"])
  def test_an_http_route_sends_the_applications_headers_from_a_middleware_of_its_own(self, name):
    text = (TEMPLATES / "traefik" / name).read_text()
    assert "- domain_${ID}_headers" in text and "customResponseHeaders: ${HEADERS}" in text

  def test_no_template_sends_headers_of_sages_own_on_an_applications_routes(self):
    assert not any("security-headers" in path.read_text() for path in (TEMPLATES / "traefik").glob("*.yml"))


class TestLeastPrivilege:
  @pytest.mark.parametrize("name", ["traefik", "cloudflared", "cloudflared-discovery", "vector", "telegraf"])
  def test_every_service_drops_its_capabilities_gains_no_privileges_and_has_a_read_only_root(self, name):
    definition = service(name)
    assert definition["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in definition["security_opt"]
    assert definition["read_only"] is True
    assert not definition.get("privileged")

  def test_only_traefik_keeps_a_capability_the_one_to_bind_its_ports(self):
    stack = yaml.safe_load((TEMPLATES / "docker-compose.yml").read_text())["services"]
    assert {name: definition["cap_add"] for name, definition in stack.items() if "cap_add" in definition} == {
        "traefik": ["NET_BIND_SERVICE"]}

  def test_traefik_reads_its_configuration_and_vector_writes_only_its_buffer(self):
    assert service("traefik")["volumes"] == ["${SAGE_HOME}/traefik:/etc/traefik:ro"]
    assert [volume for volume in service("vector")["volumes"] if not volume.endswith(":ro")] == [
        "${SAGE_HOME}/vector/buffer:/var/lib/vector"]


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


class TestTunnels:
  def test_each_cloudflared_runs_its_tunnel_from_its_own_token_in_the_env(self):
    assert service("cloudflared")["environment"] == ["TUNNEL_TOKEN=${TUNNEL_TOKEN}"]
    assert service("cloudflared-discovery")["environment"] == ["TUNNEL_TOKEN=${DISCOVERY_TUNNEL_TOKEN}"]
    env = (TEMPLATES / "worker.env").read_text().splitlines()
    assert {"TUNNEL_TOKEN=${TUNNEL_TOKEN}", "DISCOVERY_TUNNEL_TOKEN=${DISCOVERY_TUNNEL_TOKEN}"} <= set(env)

  @pytest.mark.parametrize("name", ["cloudflared", "cloudflared-discovery"])
  def test_each_serves_its_metrics_on_its_own_loopback_only(self, name):
    assert service(name)["command"].split()[:4] == ["tunnel", "--no-autoupdate", "--metrics", "127.0.0.1:20241"]


class TestTheEdge:
  def test_every_service_runs_an_exact_release(self):
    stack = yaml.safe_load((TEMPLATES / "docker-compose.yml").read_text())["services"]
    for name, definition in stack.items():
      _, _, tag = definition["image"].rpartition(":")
      assert re.fullmatch(r"v?\d+(\.\d+)+(-alpine)?", tag), f"{name} runs {definition['image']}"

  def test_traefik_reads_its_routes_from_files_only_and_has_no_docker_socket(self):
    static = yaml.safe_load((TEMPLATES / "traefik" / "traefik.yml").read_text())
    assert list(static["providers"]) == ["file"]
    assert not any("docker.sock" in volume for volume in service("traefik")["volumes"])

  def test_only_traefik_and_the_tunnels_are_on_the_edge_network_which_the_setup_creates(self):
    stack = yaml.safe_load((TEMPLATES / "docker-compose.yml").read_text())
    assert stack["networks"]["edge"] == {"name": EDGE_NETWORK, "external": True}
    assert service("traefik")["networks"] == ["default", "edge"]
    assert {name for name, definition in stack["services"].items() if "edge" in definition.get("networks", [])} == {
        "traefik", "cloudflared", "cloudflared-discovery"}
    assert service("cloudflared")["networks"] == service("cloudflared-discovery")["networks"] == ["edge"]

  def test_traefik_publishes_only_the_internal_names_ports_on_the_tailnet(self):
    assert service("traefik")["ports"] == ["${TS_IP}:443:443", "${TS_IP}:8443:8443"]


COMPOSE_TEMPLATES = ["docker-compose.yml", "application/dockerhub-compose.yml", "application/gitrepo-compose.yml"]


def compose_services(name):
  """A Compose template with every ${PLACEHOLDER} filled by a stand-in, as the manager fills them."""
  text = (TEMPLATES / name).read_text().replace("${SECRETS_BLOCK}", "")
  return yaml.safe_load(re.sub(r"\$\{(\w+)\}", "x", text))["services"]


class TestContainerLogs:
  """Docker's json-file driver keeps every line of a container's output until the container is removed."""

  @pytest.mark.parametrize("name", COMPOSE_TEMPLATES)
  def test_every_container_rotates_its_logs_the_same_way(self, name):
    services = compose_services(name)
    assert services
    for container, definition in services.items():
      assert definition["logging"] == {"driver": "json-file", "options": {"max-size": "10m", "max-file": "5"}}, container

  @pytest.mark.parametrize("name", COMPOSE_TEMPLATES)
  def test_only_an_applications_container_has_the_labels_and_it_has_all_of_them(self, name):
    labelled = {tuple(definition.get("labels", {})) for definition in compose_services(name).values()}
    assert labelled == ({tuple(LABELS)} if name.startswith("application/") else {()})


class TestApplicationPrivileges:
  @pytest.mark.parametrize("name", ["application/dockerhub-compose.yml", "application/gitrepo-compose.yml"])
  def test_an_application_gains_no_privileges_and_opens_no_raw_sockets(self, name):
    [definition] = compose_services(name).values()
    assert definition["cap_drop"] == ["NET_RAW"]
    assert definition["security_opt"] == ["no-new-privileges:true"]
    assert "cap_add" not in definition and not definition.get("privileged")


@pytest.mark.parametrize("name", ["application/dockerhub-compose.yml", "application/gitrepo-compose.yml"])
def test_a_placements_project_service_and_container_share_its_runtime_name(name):
  text = (TEMPLATES / name).read_text().replace("${SECRETS_BLOCK}", "")
  document = yaml.safe_load(re.sub(r"\$\{(\w+)\}", lambda m: "sage-0123456789abcdef" if m[1] == "RUNTIME_NAME" else "x", text))
  assert document["name"] == "sage-0123456789abcdef"
  assert {service: definition["container_name"] for service, definition in document["services"].items()} == {
      "sage-0123456789abcdef": "sage-0123456789abcdef"}

  def test_the_stack_has_the_services_that_are_capped(self):
    assert set(compose_services("docker-compose.yml")) == set(config.WORKER_STACK_SERVICES)
