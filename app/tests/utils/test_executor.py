import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar

import pytest

from utils.executor import active_executor, run_in_executor_with_context, submit_with_context

marker: ContextVar[str] = ContextVar("marker", default="unset")


@pytest.fixture
def pool():
  pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lane")
  yield pool
  pool.shutdown()


def where_and_marker(suffix=""):
  return threading.current_thread().name, marker.get() + suffix


class TestRunInExecutorWithContext:
  async def test_it_runs_on_the_given_pool_with_the_arguments(self, pool):
    thread, value = await run_in_executor_with_context(where_and_marker, suffix="!", executor=pool)
    assert thread.startswith("lane")
    assert value == "unset!"

  async def test_it_runs_on_the_pool_of_the_running_task(self, pool):
    token = active_executor.set(pool)
    try:
      thread, _ = await run_in_executor_with_context(where_and_marker)
    finally:
      active_executor.reset(token)
    assert thread.startswith("lane")

  async def test_an_explicit_pool_wins_over_the_tasks(self, pool):
    other = ThreadPoolExecutor(max_workers=1, thread_name_prefix="other")
    token = active_executor.set(pool)
    try:
      thread, _ = await run_in_executor_with_context(where_and_marker, executor=other)
    finally:
      active_executor.reset(token)
      other.shutdown()
    assert thread.startswith("other")

  async def test_it_refuses_to_run_without_a_pool(self):
    with pytest.raises(RuntimeError, match="no executor"):
      run_in_executor_with_context(where_and_marker)

  async def test_the_context_of_the_caller_reaches_the_thread(self, pool):
    token = marker.set("from the task")
    try:
      _, value = await run_in_executor_with_context(where_and_marker, executor=pool)
    finally:
      marker.reset(token)
    assert value == "from the task"

  async def test_an_error_in_the_function_reaches_the_caller(self, pool):
    def fails():
      raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
      await run_in_executor_with_context(fails, executor=pool)


class TestSubmitWithContext:
  def test_it_runs_on_the_pool_with_the_context_of_the_caller_and_needs_no_loop(self, pool):
    token = marker.set("from the caller")
    try:
      future = submit_with_context(pool, where_and_marker, "?")
    finally:
      marker.reset(token)
    thread, value = future.result(timeout=5)
    assert thread.startswith("lane")
    assert value == "from the caller?"
