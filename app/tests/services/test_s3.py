import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

import botocore.exceptions
import pytest

import config
from services import s3 as s3_module
from services.s3 import S3
from services.settings import Settings
from utils.executor import active_executor

VALUES = {"access_key": "AK", "secret_key": "SK", "bucket": "bucket", "endpoint": "https://s3.example.test", "path": "backups"}


@pytest.fixture
def unconfigured(db):
  return S3()


@pytest.fixture
def make_s3(db):
  """Writes the S3 settings, then builds S3 from them: `make_s3(bucket="other")`."""
  def make(**overrides):
    Settings().set("s3", {**VALUES, **overrides})
    return S3()
  return make


def parse(url):
  parts = urlsplit(url)
  return parts, {key: values[0] for key, values in parse_qs(parts.query).items()}


class TestLoad:
  def test_without_settings_it_is_off_and_says_so(self, db, caplog):
    with caplog.at_level(logging.WARNING, logger="services.s3"):
      s3 = S3()

    assert s3.s3_config is None
    assert s3._client is None
    assert "S3 functionality will be disabled" in caplog.text

  def test_incomplete_settings_leave_it_off(self, make_s3):
    assert make_s3(bucket="").s3_config is None

  def test_complete_settings_turn_it_on(self, make_s3):
    s3 = make_s3()

    assert s3.s3_config == VALUES
    assert s3._client.meta.endpoint_url == "https://s3.example.test"

  def test_load_follows_changed_settings(self, make_s3):
    s3 = make_s3()

    Settings().set("s3", {"bucket": "other"})
    s3.load()
    assert s3.s3_config["bucket"] == "other"

    Settings().set("s3", {"bucket": ""})
    s3.load()
    assert s3.s3_config is None
    assert s3._client is None


class TestConfigured:
  def test_check_config_exists_refuses_when_off(self, unconfigured):
    with pytest.raises(Exception, match="S3 configuration is not set"):
      unconfigured.check_config_exists()

  def test_check_config_exists_passes_when_on(self, make_s3):
    make_s3().check_config_exists()

  @pytest.mark.parametrize("base, path, full", [
      ("backups", "platform/data.db.enc", "backups/platform/data.db.enc"),
      ("/backups/", "/platform/data.db.enc/", "backups/platform/data.db.enc"),
      ("a/b", "c", "a/b/c"),
  ])
  def test_full_path_joins_the_base_path_and_the_given_one(self, make_s3, base, path, full):
    assert make_s3(path=base).get_full_path(path) == full

  def test_full_path_needs_a_configuration(self, unconfigured):
    with pytest.raises(Exception, match="S3 configuration is not set"):
      unconfigured.get_full_path("x")


class TestRegion:
  @pytest.mark.parametrize("endpoint, region", [
      ("https://s3.us-west-004.backblazeb2.com", "us-west-004"),
      ("https://s3.eu-central-003.backblazeb2.com/", "eu-central-003"),
      ("https://s3.backblazeb2.com", None),
      ("https://s3.us-west-004.backblazeb2.com.evil.test", None),
      ("https://s3.amazonaws.com", None),
      ("https://minio.example.test:9000", None),
      ("", None),
  ])
  def test_only_a_backblaze_endpoint_names_one(self, unconfigured, endpoint, region):
    assert unconfigured._get_region(endpoint) == region


class TestClientKwargs:
  def test_they_come_from_the_stored_config(self, make_s3):
    kwargs = make_s3()._get_client_kwargs()

    assert kwargs["aws_access_key_id"] == "AK"
    assert kwargs["aws_secret_access_key"] == "SK"
    assert kwargs["endpoint_url"] == "https://s3.example.test"
    assert kwargs["config"].signature_version == "s3v4"
    assert "region_name" not in kwargs

  def test_a_backblaze_endpoint_adds_its_region(self, make_s3):
    kwargs = make_s3(endpoint="https://s3.us-west-004.backblazeb2.com")._get_client_kwargs()
    assert kwargs["region_name"] == "us-west-004"

  def test_a_given_config_replaces_the_stored_one(self, make_s3):
    kwargs = make_s3()._get_client_kwargs({**VALUES, "access_key": "OTHER"})
    assert kwargs["aws_access_key_id"] == "OTHER"


class TestCheckConfig:
  @pytest.fixture
  def no_client(self, monkeypatch):
    def fail(*args, **kwargs):
      raise AssertionError("a client was built")
    monkeypatch.setattr(s3_module.boto3.session, "Session", fail)

  @pytest.mark.parametrize("config", [{}, {"bucket": ""}, dict.fromkeys(VALUES), dict.fromkeys(VALUES, "")])
  def test_a_config_with_no_values_is_valid_and_touches_nothing(self, unconfigured, no_client, config):
    assert unconfigured.check_config(config) is None

  @pytest.mark.parametrize("missing", list(VALUES))
  def test_a_partly_filled_config_is_refused(self, unconfigured, no_client, missing):
    with pytest.raises(ValueError, match="Fill in every S3 field"):
      unconfigured.check_config({**VALUES, missing: ""})


class TestPresignedUrls:
  def test_upload_url_points_at_the_key_under_the_base_path(self, make_s3):
    url = make_s3().create_presigned_upload_url("platform/data.db.enc", 600)

    parts, query = parse(url)
    assert (parts.scheme, parts.netloc) == ("https", "s3.example.test")
    assert unquote(parts.path) == "/bucket/backups/platform/data.db.enc"
    assert query["X-Amz-Expires"] == "600"
    assert query["X-Amz-Credential"].startswith("AK/")

  def test_upload_url_signs_the_content_type(self, make_s3):
    _, query = parse(make_s3().create_presigned_upload_url("x", 60))
    assert "content-type" in query["X-Amz-SignedHeaders"].split(";")

  def test_a_raw_path_is_used_as_the_key_without_the_base_path(self, make_s3):
    parts, _ = parse(make_s3().create_presigned_upload_url("/raw/key.enc/", 60, raw_path=True))
    assert unquote(parts.path) == "/bucket/raw/key.enc"

  def test_download_url_points_at_the_key_under_the_base_path(self, make_s3):
    parts, query = parse(make_s3().create_presigned_download_url("platform/data.db.enc", 60))

    assert unquote(parts.path) == "/bucket/backups/platform/data.db.enc"
    assert query["X-Amz-Expires"] == "60"
    assert "response-content-disposition" not in query

  def test_download_url_with_a_filename_asks_the_browser_to_save_it_under_that_name(self, make_s3):
    _, query = parse(make_s3().create_presigned_download_url("x", 60, filename="backup 1.enc"))
    assert query["response-content-disposition"] == 'attachment; filename="backup 1.enc"'

  def test_download_url_accepts_a_raw_path(self, make_s3):
    parts, _ = parse(make_s3().create_presigned_download_url("/raw/key.enc", 60, raw_path=True))
    assert unquote(parts.path) == "/bucket/raw/key.enc"

  def test_the_url_is_signed_for_the_region_of_a_backblaze_endpoint(self, make_s3):
    s3 = make_s3(endpoint="https://s3.us-west-004.backblazeb2.com")
    _, query = parse(s3.create_presigned_download_url("x", 60))
    assert "/us-west-004/s3/" in query["X-Amz-Credential"]

  @pytest.mark.parametrize("call", ["create_presigned_upload_url", "create_presigned_download_url"])
  def test_both_need_a_configuration(self, unconfigured, call):
    with pytest.raises(Exception, match="S3 configuration is not set"):
      getattr(unconfigured, call)("x", 60)


class TestDeleteKey:
  """Retries with the delays in the config, except for invalid credentials."""

  @pytest.fixture(autouse=True)
  def lane(self):
    """The executor a queued task would have bound."""
    pool = ThreadPoolExecutor(max_workers=1)
    token = active_executor.set(pool)
    yield
    active_executor.reset(token)
    pool.shutdown()

  @pytest.fixture
  def sleeps(self, monkeypatch):
    delays = []

    async def sleep(delay):
      delays.append(delay)

    monkeypatch.setattr(config, "S3_RETRY_DELAYS", (1, 2))
    monkeypatch.setattr(asyncio, "sleep", sleep)
    return delays

  def client_error(self):
    return botocore.exceptions.ClientError({"Error": {"Code": "SlowDown"}, "ResponseMetadata": {"HTTPStatusCode": 503}}, "DeleteObjects")

  def with_listing(self, s3, outcomes):
    """`_list_keys` returns or raises each of `outcomes` in turn; deletes answer with every key deleted."""
    s3.attempts = 0

    def list_keys(prefix):
      outcome = outcomes[min(s3.attempts, len(outcomes) - 1)]
      s3.attempts += 1
      if isinstance(outcome, Exception):
        raise outcome
      return outcome

    s3._list_keys = list_keys
    s3._client = SimpleNamespace(delete_objects=lambda Bucket, Delete: {"Deleted": Delete["Objects"]})
    return s3

  async def test_a_filter_keeps_the_other_keys(self, make_s3, sleeps):
    s3 = self.with_listing(make_s3(), [["backups/old_1", "backups/new_1"]])

    assert await s3.delete_key("x", filter=lambda key: "old_" in key) == ["backups/old_1"]

  async def test_a_failure_is_retried_after_each_delay(self, make_s3, sleeps):
    s3 = self.with_listing(make_s3(), [self.client_error(), Exception("flaky"), ["backups/a"]])

    assert await s3.delete_key("old") == ["backups/a"]
    assert s3.attempts == 3
    assert sleeps == [1, 2]

  async def test_an_s3_error_that_persists_is_reported_with_the_attempt_count(self, make_s3, sleeps):
    s3 = self.with_listing(make_s3(), [self.client_error()])

    with pytest.raises(Exception, match="Failed to delete from S3 after 3 attempts"):
      await s3.delete_key("old")
    assert s3.attempts == 3
    assert sleeps == [1, 2]

  async def test_any_other_error_that_persists_is_raised_as_it_is(self, make_s3, sleeps):
    s3 = self.with_listing(make_s3(), [ValueError("listing broke")])

    with pytest.raises(ValueError, match="listing broke"):
      await s3.delete_key("old")
    assert s3.attempts == 3

  async def test_invalid_credentials_are_not_retried(self, make_s3, sleeps):
    s3 = self.with_listing(make_s3(), [botocore.exceptions.NoCredentialsError()])

    with pytest.raises(Exception, match="S3 credentials are not valid."):
      await s3.delete_key("old")
    assert s3.attempts == 1
    assert sleeps == []

  async def test_a_key_the_bucket_refuses_to_delete_is_retried_then_raised(self, make_s3, sleeps):
    s3 = self.with_listing(make_s3(), [["backups/a"]])
    s3._client = SimpleNamespace(delete_objects=lambda Bucket, Delete: {"Errors": [{"Key": "backups/a", "Message": "AccessDenied"}]})

    with pytest.raises(Exception, match="Failed to delete backups/a: AccessDenied"):
      await s3.delete_key("old")
    assert sleeps == [1, 2]
