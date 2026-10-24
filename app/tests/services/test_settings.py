import pytest

from services.db import Setting
from services.settings import SETTING_DEFINITIONS, Settings

S3_VALUES = {"access_key": "AK", "secret_key": "SK", "bucket": "bucket", "endpoint": "https://s3.example.test", "path": "backups"}
S3_UNSET = dict.fromkeys(SETTING_DEFINITIONS["s3"])


@pytest.fixture
def settings(db):
  return Settings()


class TestLoading:
  def test_every_section_starts_with_all_its_fields_unset(self, settings):
    assert settings.get("s3") == S3_UNSET
    assert settings.get("notifications") == {"discord_webhook": None}

  def test_a_row_is_stored_for_every_defined_section(self, settings):
    assert {setting.key for setting in Setting.select()} == set(SETTING_DEFINITIONS)

  def test_rows_that_are_no_longer_defined_are_removed(self, db):
    Setting.create(key="legacy", value={"a": 1})

    Settings()

    assert not Setting.select().where(Setting.key == "legacy").exists()

  def test_missing_fields_are_added_and_unknown_ones_dropped(self, db):
    Setting.create(key="s3", value={"bucket": "kept", "legacy_field": 1})

    settings = Settings()

    expected = {**S3_UNSET, "bucket": "kept"}
    assert settings.get("s3") == expected
    assert Setting.get_by_id("s3").value == expected

  def test_load_reads_the_database_again_and_returns_a_copy(self, settings):
    stored = Setting.get_by_id("s3")
    stored.value = {**S3_UNSET, "bucket": "changed"}
    stored.save()

    loaded = settings.load("s3")

    assert loaded["bucket"] == "changed"
    assert settings.get("s3", "bucket") == "changed"
    loaded["bucket"] = "mutated"
    assert settings.get("s3", "bucket") == "changed"


class TestGet:
  def test_an_unknown_section_raises(self, settings):
    with pytest.raises(KeyError, match="Unknown setting key: nope"):
      settings.get("nope")

  def test_an_unknown_field_raises(self, settings):
    with pytest.raises(KeyError, match="Unknown setting field: nope for key: s3"):
      settings.get("s3", "nope")

  def test_a_field_gives_its_value(self, settings):
    settings.set("s3", {"bucket": "mine"})
    assert settings.get("s3", "bucket") == "mine"

  @pytest.mark.parametrize("stored", [None, ""])
  def test_an_unset_or_empty_field_gives_the_default(self, settings, stored):
    settings.set("s3", {"bucket": stored})
    assert settings.get("s3", "bucket") is None
    assert settings.get("s3", "bucket", default="fallback") == "fallback"

  def test_a_section_is_a_copy(self, settings):
    settings.get("s3")["bucket"] = "mutated"
    assert settings.get("s3", "bucket") is None


class TestSet:
  def test_only_the_given_fields_change(self, settings):
    settings.set("s3", S3_VALUES)

    settings.set("s3", {"bucket": "new"})

    assert settings.get("s3") == {**S3_VALUES, "bucket": "new"}

  def test_fields_that_are_not_defined_are_ignored(self, settings):
    settings.set("s3", {"bucket": "mine", "nope": 1})

    assert "nope" not in settings.get("s3")
    assert "nope" not in Setting.get_by_id("s3").value

  def test_an_unknown_section_raises(self, settings):
    with pytest.raises(KeyError, match="Unknown setting key: nope"):
      settings.set("nope", {})

  @pytest.mark.parametrize("value", ["text", None, ["bucket"], 3])
  def test_a_value_that_is_not_a_dict_raises(self, settings, value):
    with pytest.raises(ValueError, match="must be a dict"):
      settings.set("s3", value)

  def test_the_change_is_stored(self, settings):
    settings.set("s3", S3_VALUES)
    assert Setting.get_by_id("s3").value == S3_VALUES

  def test_secrets_are_encrypted_in_the_database(self, db, settings):
    settings.set("s3", {"secret_key": "a-very-distinctive-secret"})

    raw = db.execute_sql("SELECT value FROM setting WHERE key = 's3'").fetchone()[0]

    assert "a-very-distinctive-secret" not in str(raw)


class TestIsValid:
  def test_every_field_set_is_valid(self, settings):
    settings.set("s3", S3_VALUES)
    assert settings.is_valid("s3") is True

  def test_nothing_set_is_not_valid(self, settings):
    assert settings.is_valid("s3") is False

  @pytest.mark.parametrize("field", SETTING_DEFINITIONS["s3"])
  @pytest.mark.parametrize("empty", [None, ""])
  def test_one_field_unset_or_empty_makes_it_not_valid(self, settings, field, empty):
    settings.set("s3", {**S3_VALUES, field: empty})
    assert settings.is_valid("s3") is False

  def test_a_given_value_is_checked_instead_of_the_stored_one(self, settings):
    assert settings.is_valid("s3", S3_VALUES) is True

    settings.set("s3", S3_VALUES)
    assert settings.is_valid("s3", {"bucket": "only"}) is False

  def test_an_unknown_section_raises(self, settings):
    with pytest.raises(KeyError, match="Unknown setting key: nope"):
      settings.is_valid("nope")


class TestIsConfigured:
  def test_a_section_of_fields_that_belong_together_needs_all_of_them(self, settings):
    assert settings.is_configured("s3") is False

    settings.set("s3", {"bucket": "only"})
    assert settings.is_configured("s3") is False

    settings.set("s3", S3_VALUES)
    assert settings.is_configured("s3") is True

  def test_a_section_of_independent_channels_needs_one(self, settings):
    assert settings.is_configured("notifications") is False

    settings.set("notifications", {"discord_webhook": "https://discord.test/hook"})
    assert settings.is_configured("notifications") is True

  def test_an_independent_section_is_configured_by_any_one_field_but_valid_only_with_all(self, settings, monkeypatch):
    monkeypatch.setitem(SETTING_DEFINITIONS, "notifications", ("discord_webhook", "slack_webhook"))
    value = {"discord_webhook": "https://discord.test/hook", "slack_webhook": ""}

    assert settings.is_configured("notifications", value) is True
    assert settings.is_valid("notifications", value) is False

  def test_a_given_value_is_checked_instead_of_the_stored_one(self, settings):
    assert settings.is_configured("s3", S3_VALUES) is True
    assert settings.is_configured("notifications", {"discord_webhook": "https://discord.test/hook"}) is True
    assert settings.is_configured("notifications", {"discord_webhook": ""}) is False
