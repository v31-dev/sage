from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from services.db import Application, Project
from utils.api import generic_create, generic_delete, generic_get, generic_list, generic_update, get_request_models, parse_api_data


class Broken:
  """A model whose queries all fail, for the 500 paths."""

  @classmethod
  def select(cls, *args, **kwargs):
    raise RuntimeError("db is down")

  create = get_or_none = select


class Failing:
  """A model instance that cannot be saved or deleted."""

  def save(self):
    raise RuntimeError("disk full")

  def delete_instance(self):
    raise RuntimeError("database is locked")


def request():
  return SimpleNamespace(state=SimpleNamespace())


def raised(call, *args, **kwargs):
  with pytest.raises(HTTPException) as caught:
    call(*args, **kwargs)
  return caught.value


class TestGetRequestModels:
  def test_the_first_call_creates_the_cache_on_the_request(self):
    incoming = request()

    models = get_request_models(incoming)

    assert models == {}
    assert incoming.state.models is models

  def test_later_calls_share_the_same_cache(self):
    incoming = request()
    get_request_models(incoming)["project"] = "demo"

    assert get_request_models(incoming) is incoming.state.models
    assert get_request_models(incoming)["project"] == "demo"

  def test_required_models_that_are_present_pass(self):
    incoming = request()
    get_request_models(incoming).update(project="demo", application="web")

    assert get_request_models(incoming, ["project", "application"])["application"] == "web"

  def test_a_missing_required_model_is_a_404_naming_it(self):
    error = raised(get_request_models, request(), ["application"])
    assert (error.status_code, error.detail) == (404, "application not found")

  def test_the_first_missing_one_is_named(self):
    incoming = request()
    get_request_models(incoming)["project"] = "demo"

    assert raised(get_request_models, incoming, ["project", "application", "volume"]).detail == "application not found"

  def test_a_required_model_that_is_none_is_a_404(self):
    incoming = request()
    get_request_models(incoming)["application"] = None

    assert raised(get_request_models, incoming, ["application"]).status_code == 404

  def test_none_can_be_allowed_but_a_missing_key_still_is_not(self):
    incoming = request()
    get_request_models(incoming)["application"] = None

    assert get_request_models(incoming, ["application"], none_allowed=True)["application"] is None
    assert raised(get_request_models, incoming, ["volume"], none_allowed=True).status_code == 404


class TestGenericList:
  def test_every_row_as_a_dict(self, project):
    Project.create(name="other", label="Other")

    assert [row["name"] for row in generic_list(Project)] == ["demo", "other"]

  def test_a_condition_narrows_it(self, project):
    Project.create(name="other", label="Other")
    assert [row["name"] for row in generic_list(Project, Project.name == "other")] == ["other"]

  def test_no_rows_is_an_empty_list(self, db):
    assert generic_list(Project) == []

  def test_a_failure_is_a_500_naming_the_model_and_the_cause(self):
    error = raised(generic_list, Broken)
    assert error.status_code == 500
    assert error.detail == "Failed to list Broken with error: db is down"


class TestGenericCreate:
  def test_it_creates_the_row_and_returns_it_as_a_dict(self, db):
    result = generic_create(Project, {"name": "demo", "label": "Demo"})

    assert result["name"] == "demo"
    assert result["label"] == "Demo"
    assert Project.get_by_id("demo").label == "Demo"

  def test_a_value_the_model_refuses_is_a_400_with_its_message(self, db):
    error = raised(generic_create, Project, {"name": "Not Valid", "label": "x"})

    assert error.status_code == 400
    assert "Invalid name" in error.detail
    assert Project.select().count() == 0

  def test_a_duplicate_is_a_409(self, project):
    error = raised(generic_create, Project, {"name": "demo", "label": "Again"})

    assert error.status_code == 409
    assert error.detail.startswith("Conflict:")

  def test_any_other_failure_is_a_500(self):
    error = raised(generic_create, Broken, {})
    assert (error.status_code, error.detail) == (500, "Failed to create Broken with error: db is down")


class TestGenericGet:
  def test_it_returns_the_row_as_a_dict(self, project):
    assert generic_get(Project, Project.name == "demo")["label"] == "Demo"

  def test_it_can_return_the_model_itself(self, project):
    found = generic_get(Project, Project.name == "demo", return_model=True)

    assert isinstance(found, Project)
    assert found.label == "Demo"

  def test_no_row_is_a_404_naming_the_model(self, db):
    error = raised(generic_get, Project, Project.name == "missing")
    assert (error.status_code, error.detail) == (404, "Project not found")

  def test_a_value_the_model_refuses_is_a_400(self, db):
    error = raised(generic_get, Project, Project.name == "Not Valid")

    assert error.status_code == 400
    assert "Invalid name" in error.detail

  def test_any_other_failure_is_a_500(self):
    error = raised(generic_get, Broken, None)
    assert (error.status_code, error.detail) == (500, "Failed to fetch Broken with error: db is down")


class TestGenericUpdate:
  def test_it_sets_the_fields_saves_and_returns_the_row(self, project):
    result = generic_update(Project, project, {"label": "Renamed", "description": "Notes"})

    assert (result["label"], result["description"]) == ("Renamed", "Notes")
    saved = Project.get_by_id("demo")
    assert (saved.label, saved.description) == ("Renamed", "Notes")

  def test_no_instance_is_a_404(self, db):
    error = raised(generic_update, Project, None, {"label": "x"})
    assert (error.status_code, error.detail) == (404, "Project not found")

  def test_a_value_the_model_refuses_is_a_400_and_nothing_is_stored(self, project):
    error = raised(generic_update, Project, project, {"env": "9BAD=1"})

    assert error.status_code == 400
    assert "project.env" in error.detail
    assert Project.get_by_id("demo").env is None

  def test_a_clash_with_another_row_is_a_409(self, project, application):
    other = Application.create(project=project, name="api", label="Api")

    error = raised(generic_update, Application, other, {"name": "web"})

    assert error.status_code == 409
    assert error.detail.startswith("Conflict:")

  def test_any_other_failure_is_a_500(self):
    error = raised(generic_update, Broken, Failing(), {})
    assert (error.status_code, error.detail) == (500, "Failed to update Broken with error: disk full")


class TestGenericDelete:
  def test_it_deletes_the_row(self, project):
    assert generic_delete(Project, project) == {"status": "deleted"}
    assert Project.select().count() == 0

  def test_no_instance_is_a_404(self, db):
    error = raised(generic_delete, Project, None)
    assert (error.status_code, error.detail) == (404, "Project not found")

  def test_a_row_the_database_refuses_to_delete_is_a_409_and_stays(self, project, application):
    error = raised(generic_delete, Project, project)

    assert error.status_code == 409
    assert error.detail.startswith("Conflict:")
    assert Project.get_by_id("demo").label == "Demo"

  def test_any_other_failure_is_a_500(self):
    error = raised(generic_delete, Broken, Failing())
    assert (error.status_code, error.detail) == (500, "Failed to delete Broken with error: database is locked")


class TestParseApiData:
  def test_it_keeps_only_the_listed_keys_that_are_present(self):
    assert parse_api_data({"label": "Web", "other": 1}, ["label", "description"]) == {"label": "Web"}

  def test_strings_are_stripped(self):
    assert parse_api_data({"label": "  Web  ", "env": "\nA=1\n"}, ["label", "env"]) == {"label": "Web", "env": "A=1"}

  def test_a_blank_string_becomes_empty(self):
    assert parse_api_data({"label": "   "}, ["label"]) == {"label": ""}

  def test_other_types_are_left_as_they_are(self):
    data = {"port": 80, "nothing": None, "flag": False, "items": [" a "]}
    assert parse_api_data(data, list(data)) == data
