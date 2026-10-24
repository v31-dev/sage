import asyncio
import time
from typing import Awaitable, Callable

import botocore.exceptions
import httpx
import requests

# Network failures that clear by themselves.
NETWORK_ERRORS = (
    httpx.TransportError,
    botocore.exceptions.ConnectionError,
    botocore.exceptions.ReadTimeoutError,
    requests.Timeout,
    requests.ConnectionError,
)


def is_transient(error: Exception) -> bool:
  """Whether a failure clears by itself, so trying again can work: the error says so (a
  `transient` attribute), it is a network failure, or the service answered with a server
  error (HTTP 5xx, and 429 for a webhook)."""
  flag = getattr(error, "transient", None)
  if flag is not None:
    return flag
  if isinstance(error, NETWORK_ERRORS):
    return True
  if isinstance(error, botocore.exceptions.ClientError):
    return error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0) >= 500
  if isinstance(error, requests.HTTPError):
    return error.response.status_code >= 500 or error.response.status_code == 429
  return False


def retry(call: Callable, *, delays, should_retry: Callable[[Exception], bool] = is_transient,
          on_retry: Callable[[Exception, float], None] | None = None, sleep: Callable[[float], None] | None = None):
  """Return `call()`. When it raises an error that `should_retry` accepts, wait the next of
  `delays` (seconds) and call it again. The error of the last attempt, or one that should not
  be retried, is raised. `on_retry(error, delay)` is called before each wait."""
  for delay in (*delays, None):
    try:
      return call()
    except Exception as error:
      if delay is None or not should_retry(error):
        raise
      if on_retry:
        on_retry(error, delay)
      (sleep or time.sleep)(delay)


async def retry_async(call: Callable[[], Awaitable], *, delays, should_retry: Callable[[Exception], bool] = is_transient,
                      on_retry: Callable[[Exception, float], None] | None = None, sleep: Callable[[float], Awaitable] | None = None):
  """`retry` for a coroutine function."""
  for delay in (*delays, None):
    try:
      return await call()
    except Exception as error:
      if delay is None or not should_retry(error):
        raise
      if on_retry:
        on_retry(error, delay)
      await (sleep or asyncio.sleep)(delay)
