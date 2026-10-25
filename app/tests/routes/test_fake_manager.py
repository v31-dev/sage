import inspect

import pytest

from services.manager import Manager
from services.s3 import S3
from tests.routes.fakes import FakeManager, FakeS3


def parameters(function):
  return list(inspect.signature(function).parameters)


@pytest.mark.parametrize("name", ["key_exists", "create_presigned_download_url"])
def test_the_fake_s3_takes_the_same_arguments_as_the_real_one(name):
  assert parameters(getattr(FakeS3, name)) == parameters(getattr(S3, name))


@pytest.mark.parametrize("name", ["task_state", "is_task_running", "has_task", "worker_status", "get_volume_backup_resource_error"])
def test_the_fake_manager_takes_the_same_arguments_as_the_real_one(name):
  assert parameters(getattr(FakeManager, name)) == parameters(getattr(Manager, name))


def test_a_method_the_real_manager_lacks_raises():
  with pytest.raises(AttributeError, match="no_such_task"):
    FakeManager().no_such_task


def test_a_task_the_route_asks_for_wrongly_is_refused():
  manager = FakeManager()

  with pytest.raises(TypeError):
    manager.add_task(task=manager.backup_database_s3, scopes={"platform"}, executor="platform", not_a_parameter=1)

  with pytest.raises(TypeError):
    manager.add_task(task=manager.delete_backup_s3, scopes={"common"}, executor="common", params={"wrong": 1})
