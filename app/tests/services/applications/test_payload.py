from services.applications.payload import application_payload, application_summary, project_list, project_payload
from services.db.models import Project, Volume


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


def test_a_project_is_its_configuration_and_how_many_applications_it_has(db):
  payload = project_payload(Project.create(name="demo", label="Demo", env="A=1", application_count=2))
  assert {key: payload[key] for key in ("name", "label", "env", "application_count")} == {
      "name": "demo", "label": "Demo", "env": "A=1", "application_count": 2}


def test_the_projects_are_listed_by_name(db):
  for name in ("zeta", "alpha"):
    Project.create(name=name, label=name.title())
  assert [project["name"] for project in project_list()] == ["alpha", "zeta"]


def test_an_applications_summary_has_what_it_runs_and_how_many_of_its_containers_are_active(application):
  summary = application_summary(application, 3)
  assert {key: summary[key] for key in ("name", "label", "type", "image", "container_count", "active_count")} == {
      "name": "web", "label": "Web", "type": "docker", "image": "nginx", "container_count": 0, "active_count": 3}
  assert "env" not in summary and "containers" not in summary
