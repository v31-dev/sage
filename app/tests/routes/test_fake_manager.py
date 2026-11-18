import pytest

from tests.routes.fakes import ManagerStandIn


def test_a_call_is_recorded_and_answered_with_a_task_id():
  manager = ManagerStandIn()
  assert manager.request_release_check(quiet=False) == "task-1"
  assert manager.called("request_release_check") == [((), {"quiet": False})]


def test_an_answer_is_returned_raised_or_called_with_the_arguments():
  manager = ManagerStandIn()
  manager.answers = {"info": {"version": "1"}, "request_reboot": LookupError("Worker not found."),
                     "worker_page": lambda hostname: {"hostname": hostname}}
  assert manager.info() == {"version": "1"}
  with pytest.raises(LookupError):
    manager.request_reboot("w1")
  assert manager.worker_page("w2") == {"hostname": "w2"}


def test_a_static_method_is_called_as_the_real_one_takes_it():
  manager = ManagerStandIn()
  manager.describe_check_error(TimeoutError())
  with pytest.raises(TypeError):
    manager.describe_check_error()
  assert [name for name, *_ in manager.calls] == ["describe_check_error"]


def test_a_method_the_real_manager_lacks_raises():
  with pytest.raises(AttributeError, match="no_such_request"):
    ManagerStandIn().no_such_request


def test_a_call_the_real_method_does_not_take_is_refused():
  manager = ManagerStandIn()
  with pytest.raises(TypeError):
    manager.request_reboot()
  with pytest.raises(TypeError):
    manager.request_release_check(wrong=True)
  assert manager.calls == []
