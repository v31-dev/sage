from services.applications.payload import application_payload
from services.db.models import Volume


def container(headline):
  return {"view": {"headline": headline}}


def test_an_application_is_its_configuration_volumes_and_containers_with_how_many_are_active(application):
  volume = Volume.create(application=application, name="data", path="/data", backup_cron="0 3 * * *")
  containers = [container("active"), container("deploying"), container("active"), container("error")]
  payload = application_payload(application, [volume], containers)
  assert {key: payload[key] for key in ("name", "label", "type", "image", "project")} == {
      "name": "web", "label": "Web", "type": "docker", "image": "nginx", "project": {"name": "demo", "label": "Demo"}}
  assert [(item["id"], item["name"], item["path"], item["backup_cron"]) for item in payload["volumes"]] == [
      (volume.id, "data", "/data", "0 3 * * *")]
  assert (payload["containers"], payload["active_count"]) == (containers, 2)


def test_an_application_without_containers_has_none_active(application):
  assert application_payload(application, [], [])["active_count"] == 0
