from datetime import datetime, timedelta, timezone

import pytest

from utils.common import get_env, utcnow


class TestUtcnow:
  def test_it_is_naive(self):
    assert utcnow().tzinfo is None

  @pytest.mark.parametrize("zone", ["UTC", "Asia/Kolkata", "America/Los_Angeles", "Pacific/Auckland"])
  def test_it_is_utc_whatever_zone_the_machine_is_in(self, machine_timezone, zone):
    machine_timezone(zone)

    assert abs(utcnow() - datetime.now(timezone.utc).replace(tzinfo=None)) < timedelta(seconds=5)

  def test_it_differs_from_the_local_time_when_the_machine_is_not_on_utc(self, machine_timezone):
    machine_timezone("Asia/Kolkata")

    local = datetime.now()  # noqa: DTZ005 -- the local clock is what utcnow() must not return
    assert abs((local - utcnow()) - timedelta(hours=5, minutes=30)) < timedelta(seconds=5)

  def test_the_clock_fixture_freezes_it_until_moved(self, clock):
    assert utcnow() == datetime(2026, 1, 1, 12, 0, 0)

    clock.advance(timedelta(minutes=90))
    assert utcnow() == datetime(2026, 1, 1, 13, 30, 0)

    clock.set(datetime(2030, 6, 1))
    assert utcnow() == datetime(2030, 6, 1)


class TestGetEnv:
  def test_returns_the_value(self, monkeypatch):
    monkeypatch.setenv("SAGE_TEST_VALUE", "set")
    assert get_env("SAGE_TEST_VALUE") == "set"

  def test_the_value_wins_over_the_fallback(self, monkeypatch):
    monkeypatch.setenv("SAGE_TEST_VALUE", "set")
    assert get_env("SAGE_TEST_VALUE", "fallback") == "set"

  @pytest.mark.parametrize("value", [None, ""])
  def test_unset_or_empty_uses_the_fallback(self, monkeypatch, value):
    if value is None:
      monkeypatch.delenv("SAGE_TEST_VALUE", raising=False)
    else:
      monkeypatch.setenv("SAGE_TEST_VALUE", value)
    assert get_env("SAGE_TEST_VALUE", "fallback") == "fallback"

  @pytest.mark.parametrize("value", [None, ""])
  def test_unset_or_empty_without_a_fallback_raises_naming_the_variable(self, monkeypatch, value):
    if value is None:
      monkeypatch.delenv("SAGE_TEST_VALUE", raising=False)
    else:
      monkeypatch.setenv("SAGE_TEST_VALUE", value)
    with pytest.raises(ValueError, match="SAGE_TEST_VALUE"):
      get_env("SAGE_TEST_VALUE")
