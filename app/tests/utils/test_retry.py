from types import SimpleNamespace

import botocore.exceptions
import httpx
import pytest
import requests

from utils.retry import is_transient, retry, retry_async


class Flaky:
  """Raises `errors` in turn, then returns `result`; `calls` counts the attempts."""

  def __init__(self, *errors, result="done"):
    self.errors = list(errors)
    self.result = result
    self.calls = 0

  def __call__(self):
    self.calls += 1
    if self.errors:
      raise self.errors.pop(0)
    return self.result

  async def acall(self):
    return self()


class Transient(Exception):
  transient = True


class Permanent(Exception):
  transient = False


def client_error(status):
  return botocore.exceptions.ClientError({"Error": {}, "ResponseMetadata": {"HTTPStatusCode": status}}, "DeleteObjects")


class TestIsTransient:
  def test_an_error_that_says_so_is(self):
    assert is_transient(Transient()) is True

  def test_an_error_that_says_it_is_not_is_not_even_when_it_is_a_network_error(self):
    class Refused(httpx.ConnectError):
      transient = False

    assert is_transient(Permanent()) is False
    assert is_transient(Refused("refused")) is False

  @pytest.mark.parametrize("error", [
      httpx.ConnectError("refused"),
      httpx.ReadTimeout("slow"),
      botocore.exceptions.EndpointConnectionError(endpoint_url="https://s3.example.test"),
      botocore.exceptions.ReadTimeoutError(endpoint_url="https://s3.example.test"),
      requests.Timeout(),
      requests.ConnectionError(),
  ])
  def test_network_failures_are(self, error):
    assert is_transient(error) is True

  @pytest.mark.parametrize("status, transient", [(500, True), (503, True), (429, False), (404, False), (403, False)])
  def test_an_s3_error_is_by_its_status(self, status, transient):
    assert is_transient(client_error(status)) is transient

  def test_an_s3_error_without_a_status_is_not(self):
    assert is_transient(botocore.exceptions.ClientError({"Error": {}}, "DeleteObjects")) is False

  @pytest.mark.parametrize("status, transient", [(500, True), (502, True), (429, True), (404, False), (400, False)])
  def test_a_webhook_error_is_by_its_status(self, status, transient):
    assert is_transient(requests.HTTPError(response=SimpleNamespace(status_code=status))) is transient

  @pytest.mark.parametrize("error", [ValueError("bad"), KeyError("k"), Exception("boom")])
  def test_anything_else_is_not(self, error):
    assert is_transient(error) is False


class TestRetry:
  def test_a_call_that_works_is_not_waited_for(self):
    sleeps = []

    assert retry(Flaky(), delays=(1, 2), sleep=sleeps.append) == "done"
    assert sleeps == []

  def test_each_failure_waits_the_next_delay_and_tries_again(self):
    sleeps = []
    call = Flaky(Transient(), Transient())

    assert retry(call, delays=(1, 2, 4), sleep=sleeps.append) == "done"
    assert call.calls == 3
    assert sleeps == [1, 2]

  def test_the_error_of_the_last_attempt_is_raised(self):
    sleeps = []
    errors = [Transient("first"), Transient("second"), Transient("last")]
    call = Flaky(*errors)

    with pytest.raises(Transient, match="last"):
      retry(call, delays=(1, 2), sleep=sleeps.append)
    assert call.calls == 3
    assert sleeps == [1, 2]

  def test_an_error_that_is_not_retried_is_raised_at_once(self):
    sleeps = []
    call = Flaky(ValueError("bad"))

    with pytest.raises(ValueError):
      retry(call, delays=(1, 2), sleep=sleeps.append)
    assert call.calls == 1
    assert sleeps == []

  def test_the_default_is_to_retry_what_is_transient(self):
    sleeps = []

    assert retry(Flaky(httpx.ConnectError("refused")), delays=(1,), sleep=sleeps.append) == "done"
    assert sleeps == [1]

  def test_the_predicate_decides(self):
    sleeps = []
    call = Flaky(ValueError("retry me"), ValueError("not me"))

    with pytest.raises(ValueError, match="not me"):
      retry(call, delays=(1, 2, 3), should_retry=lambda error: "retry me" in str(error), sleep=sleeps.append)
    assert sleeps == [1]

  def test_the_notice_comes_before_each_wait(self):
    events = []
    first, second = Transient("first"), Transient("second")

    retry(Flaky(first, second), delays=(5, 10), on_retry=lambda error, delay: events.append(("notice", error, delay)),
          sleep=lambda delay: events.append(("sleep", delay)))

    assert events == [("notice", first, 5), ("sleep", 5), ("notice", second, 10), ("sleep", 10)]

  def test_without_delays_it_is_one_attempt(self):
    call = Flaky(Transient())

    with pytest.raises(Transient):
      retry(call, delays=())
    assert call.calls == 1

  def test_it_sleeps_with_time_sleep_by_default(self, monkeypatch):
    import time
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)

    retry(Flaky(Transient()), delays=(7,))

    assert sleeps == [7]

  def test_an_interrupt_is_not_caught(self):
    with pytest.raises(KeyboardInterrupt):
      retry(Flaky(KeyboardInterrupt()), delays=(1,), should_retry=lambda error: True, sleep=lambda delay: None)


class TestRetryAsync:
  async def test_each_failure_waits_the_next_delay_and_tries_again(self):
    sleeps = []

    async def sleep(delay):
      sleeps.append(delay)

    call = Flaky(Transient(), Transient())

    assert await retry_async(call.acall, delays=(1, 2, 4), sleep=sleep) == "done"
    assert call.calls == 3
    assert sleeps == [1, 2]

  async def test_the_error_of_the_last_attempt_is_raised(self):
    async def sleep(delay):
      pass

    call = Flaky(Transient("first"), Transient("last"))

    with pytest.raises(Transient, match="last"):
      await retry_async(call.acall, delays=(1,), sleep=sleep)
    assert call.calls == 2

  async def test_an_error_that_is_not_retried_is_raised_at_once(self):
    call = Flaky(ValueError("bad"))

    with pytest.raises(ValueError):
      await retry_async(call.acall, delays=(1, 2))
    assert call.calls == 1

  async def test_the_predicate_and_the_notice_work_as_in_the_sync_form(self):
    events = []

    async def sleep(delay):
      events.append(("sleep", delay))

    call = Flaky(ValueError("retry me"), ValueError("not me"))

    with pytest.raises(ValueError, match="not me"):
      await retry_async(call.acall, delays=(3, 6), should_retry=lambda error: "retry me" in str(error),
                        on_retry=lambda error, delay: events.append(("notice", delay)), sleep=sleep)
    assert events == [("notice", 3), ("sleep", 3)]

  async def test_it_sleeps_with_asyncio_sleep_by_default(self, monkeypatch):
    import asyncio
    sleeps = []

    async def fake_sleep(delay):
      sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await retry_async(Flaky(Transient()).acall, delays=(9,))

    assert sleeps == [9]
