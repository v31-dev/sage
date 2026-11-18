import pytest

from services.db.models import Setting
from services.notification import CHANNELS
from services.settings import SETTING_DEFINITIONS, SettingCheck, Settings, section_status, setting_payload, validate_section

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


def ok(revision, message="Bucket reachable.", channels=None):
  return SettingCheck(revision, "ok", message, "2026-01-01T12:00:00", channels)


class TestRevisions:
  def test_every_save_and_reload_of_a_section_moves_its_revision_on_alone(self, settings):
    before = settings.snapshot("s3").revision
    other = settings.snapshot("notifications").revision

    settings.set("s3", S3_VALUES)
    assert settings.snapshot("s3").revision == before + 1
    settings.load("s3")
    assert settings.snapshot("s3").revision == before + 2
    assert settings.snapshot("notifications").revision == other

  def test_reloading_everything_moves_every_section_on(self, settings):
    before = {key: settings.snapshot(key).revision for key in SETTING_DEFINITIONS}
    settings.load_all()
    assert all(settings.snapshot(key).revision == before[key] + 1 for key in SETTING_DEFINITIONS)

  def test_a_snapshot_is_a_copy_of_the_saved_values(self, settings):
    settings.set("s3", S3_VALUES)
    snapshot = settings.snapshot("s3")
    snapshot.values["bucket"] = "changed"
    assert settings.get("s3", "bucket") == "bucket"

  def test_a_required_env_section_is_revision_zero_with_no_values(self, settings):
    assert (settings.snapshot("tailscale").revision, settings.snapshot("tailscale").values) == (0, {})


class TestChecks:
  def test_a_check_of_the_saved_revision_is_kept_and_read(self, settings):
    revision = settings.snapshot("s3").revision
    assert settings.record_check("s3", ok(revision)) is True
    assert settings.check("s3") == ok(revision)

  def test_a_check_of_an_older_revision_is_dropped(self, settings):
    revision = settings.snapshot("s3").revision
    settings.set("s3", S3_VALUES)
    assert settings.record_check("s3", ok(revision)) is False
    assert settings.check("s3") is None

  def test_a_save_after_the_check_leaves_no_current_check_until_the_next(self, settings):
    settings.record_check("s3", ok(settings.snapshot("s3").revision))
    settings.set("s3", S3_VALUES)
    assert settings.check("s3") is None
    settings.record_check("s3", ok(settings.snapshot("s3").revision, "Again."))
    assert settings.check("s3").message == "Again."

  def test_a_restore_reload_leaves_no_check_of_what_was_saved_before(self, settings):
    settings.record_check("notifications", ok(settings.snapshot("notifications").revision))
    settings.load_all()
    assert settings.check("notifications") is None

  def test_an_env_section_keeps_its_check(self, settings):
    settings.record_check("tailscale", ok(0, "The node is running."))
    settings.load_all()
    assert settings.check("tailscale").message == "The node is running."


class TestValidateSection:
  @pytest.mark.parametrize("values", [S3_VALUES, S3_UNSET, dict.fromkeys(SETTING_DEFINITIONS["s3"], ""), {}])
  def test_s3_complete_or_empty_is_accepted(self, values):
    validate_section("s3", values)

  @pytest.mark.parametrize("missing", SETTING_DEFINITIONS["s3"])
  def test_s3_partly_filled_is_refused(self, missing):
    with pytest.raises(ValueError, match="Fill in every field, or leave them all empty."):
      validate_section("s3", {**S3_VALUES, missing: ""})

  def test_each_notification_channel_stands_alone(self):
    validate_section("notifications", {"discord_webhook": "https://discord.test/x"})
    validate_section("notifications", {"discord_webhook": None})

  def test_a_field_the_section_does_not_have_is_refused(self):
    with pytest.raises(ValueError, match="Unknown fields: region, zone."):
      validate_section("s3", {**S3_VALUES, "zone": "z", "region": "eu"})

  def test_a_field_that_is_not_text_is_refused(self):
    with pytest.raises(ValueError, match="bucket must be text."):
      validate_section("s3", {**S3_VALUES, "bucket": 5})

  def test_an_unknown_section_is_refused(self):
    with pytest.raises(KeyError):
      validate_section("tailscale", {})


def test_the_notification_fields_are_its_channels():
  assert SETTING_DEFINITIONS["notifications"] == tuple(CHANNELS)


CERTIFICATES = [{"id": "manager", "names": ["manager.core.example.test"], "valid": True, "error": None, "expiry": "2026-03-01T00:00:00"},
                {"id": "discovery", "names": ["discovery.int.example.test"], "valid": True, "error": None,
                 "expiry": "2026-02-15T00:00:00"}]


class TestSectionStatus:
  def test_a_section_not_configured_shows_nothing_of_a_check(self):
    assert section_status(False, None, ok(1)) == {"state": "not_configured", "message": None, "task_id": None,
                                                  "checked_at": "2026-01-01T12:00:00", "details": None, "channels": None}

  def test_a_task_that_makes_it_apply_shows_applying_with_the_task(self):
    status = section_status(True, "t1", ok(1))
    assert (status["state"], status["task_id"]) == ("applying", "t1")

  def test_before_its_first_check_of_what_is_saved_it_shows_checking(self):
    assert (section_status(True, None, None)["state"], section_status(True, None, None)["message"]) == ("applying", "Checking…")

  def test_otherwise_its_check(self):
    failed = SettingCheck(1, "error", "Webhook invalid", "2026-01-01T12:00:00", {"discord_webhook": "404"})
    assert section_status(True, None, failed) == {"state": "error", "message": "Webhook invalid", "task_id": None,
                                                  "checked_at": "2026-01-01T12:00:00", "details": None,
                                                  "channels": {"discord_webhook": "404"}}

  def test_cloudflare_adds_the_earliest_certificate_expiry_to_a_good_check(self):
    status = section_status(True, None, ok(0, "Verified."), CERTIFICATES)
    assert (status["state"], status["message"]) == ("ok", "Verified. Certificates valid until 2026-02-15.")

  def test_a_certificate_that_needs_attention_turns_a_good_check_into_an_error(self):
    broken = [{**CERTIFICATES[0], "error": "order rejected"}, {**CERTIFICATES[1], "valid": False}]
    status = section_status(True, None, ok(0, "Verified."), broken)
    assert status["state"] == "error"
    assert status["message"] == ("Certificates need attention: manager.core.example.test: order rejected; "
                                 "discovery.int.example.test: not valid")


class TestPayloads:
  def test_a_saved_section_is_its_row_with_its_values_readable(self, settings):
    settings.set("s3", S3_VALUES)
    assert (setting_payload("s3")["key"], setting_payload("s3")["value"]) == ("s3", S3_VALUES)

  def test_a_section_never_saved_has_its_row_with_every_field_unset(self, settings):
    assert setting_payload("notifications")["value"] == {field: None for field in SETTING_DEFINITIONS["notifications"]}

  def test_an_unknown_section_is_a_lookup_error(self, settings):
    with pytest.raises(LookupError):
      setting_payload("nope")
