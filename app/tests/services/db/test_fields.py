import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken
from peewee import Check, IntegrityError, Model, SqliteDatabase

from services.db import encryption
from services.db.fields import (AlphaNumericField, ChoiceField, CleanCharField, EncryptedJSONField, EncryptedKeyValueField, EncryptedTextField,
                                JSONField, LabelField, OriginListField)
from values.names import derive_name


@pytest.fixture(autouse=True)
def initialized_encryption(monkeypatch):
  monkeypatch.setattr(encryption, "_cipher", None)
  encryption.initialize_encryption(os.environ["ENCRYPTION_KEY"])


class TestCleanCharField:
  @pytest.mark.parametrize("value", ["abc", "a-b", "123", "a1-b2-c3", None])
  def test_accepts_lowercase_digits_and_dashes(self, value):
    assert CleanCharField().db_value(value) == value

  @pytest.mark.parametrize("value", ["Abc", "a_b", "a b", "a.b", "", "ä"])
  def test_rejects_anything_else(self, value):
    with pytest.raises(ValueError, match="Invalid name"):
      CleanCharField().db_value(value)


class TestAlphaNumericField:
  @pytest.mark.parametrize("value", ["a", "abc", "a1", "web2"])
  def test_validate_accepts_lowercase_letters_and_digits_starting_with_a_letter(self, value):
    assert AlphaNumericField().validate(value) == value

  def test_validate_lets_none_through(self):
    assert AlphaNumericField().validate(None) is None

  @pytest.mark.parametrize("value", ["1abc", "Abc", "a-b", "a_b", "a b", ""])
  def test_validate_rejects_anything_else(self, value):
    with pytest.raises(ValueError, match="Invalid name"):
      AlphaNumericField().validate(value)

  def test_db_value_validates(self):
    with pytest.raises(ValueError):
      AlphaNumericField().db_value("not valid")

  def test_a_value_longer_than_the_field_allows_is_refused_naming_the_limit(self):
    field = AlphaNumericField(max_length=10)
    assert field.validate("a" * 10) == "a" * 10
    with pytest.raises(ValueError, match="at most 10 characters"):
      field.validate("a" * 11)

  def test_a_field_without_a_limit_of_its_own_allows_255(self):
    assert AlphaNumericField().validate("a" * 255) == "a" * 255
    with pytest.raises(ValueError, match="at most 255 characters"):
      AlphaNumericField().validate("a" * 256)

  @pytest.mark.parametrize("label, name", [
      ("My App", "myapp"),
      ("My App 2!", "myapp2"),
      ("  Web-Server_01 ", "webserver01"),
      ("a", "a"),
  ])
  def test_clean_derives_a_name_from_a_label(self, label, name):
    assert derive_name(label) == name

  @pytest.mark.parametrize("label", ["", None, "123", "1app", "!!!", "   "])
  def test_clean_refuses_a_label_with_no_valid_name(self, label):
    with pytest.raises(ValueError, match="does not produce a valid name"):
      derive_name(label)


class TestEncryptedTextField:
  def test_round_trip(self):
    field = EncryptedTextField()
    assert field.python_value(field.db_value("a secret")) == "a secret"

  def test_the_stored_value_is_not_the_plain_text(self):
    stored = EncryptedTextField().db_value("a secret")
    assert "secret" not in stored

  def test_none_stays_none_both_ways(self):
    field = EncryptedTextField()
    assert field.db_value(None) is None
    assert field.python_value(None) is None

  def test_empty_string_round_trips(self):
    field = EncryptedTextField()
    assert field.python_value(field.db_value("")) == ""

  def test_non_ascii_round_trips(self):
    field = EncryptedTextField()
    assert field.python_value(field.db_value("pässwörd ✓")) == "pässwörd ✓"

  def test_the_same_text_is_stored_differently_each_time(self):
    field = EncryptedTextField()
    assert field.db_value("x") != field.db_value("x")

  def test_a_value_made_with_another_key_cannot_be_read(self):
    foreign = Fernet(Fernet.generate_key()).encrypt(b"secret").decode()
    with pytest.raises(InvalidToken):
      EncryptedTextField().python_value(foreign)


class TestEncryptedJSONField:
  @pytest.mark.parametrize("value", [{"a": 1}, {"nested": {"b": [1, 2, {"c": None}]}}, [1, "two", None], {}, []])
  def test_round_trip(self, value):
    field = EncryptedJSONField()
    assert field.python_value(field.db_value(value)) == value

  def test_none_stays_none_both_ways(self):
    field = EncryptedJSONField()
    assert field.db_value(None) is None
    assert field.python_value(None) is None

  def test_the_stored_value_is_not_readable(self):
    assert "token" not in EncryptedJSONField().db_value({"token": "abc"})

  def test_text_that_is_already_json_is_encrypted_as_given(self):
    field = EncryptedJSONField()
    assert field.python_value(field.db_value('{"a": 1}')) == {"a": 1}

  def test_a_value_made_with_another_key_cannot_be_read(self):
    foreign = Fernet(Fernet.generate_key()).encrypt(b'{"a": 1}').decode()
    with pytest.raises(InvalidToken):
      EncryptedJSONField().python_value(foreign)


class TestJSONField:
  def test_stores_json_text(self):
    assert json.loads(JSONField().db_value({"a": [1, 2]})) == {"a": [1, 2]}

  def test_round_trip(self):
    field = JSONField()
    assert field.python_value(field.db_value({"a": [1, None]})) == {"a": [1, None]}

  def test_none_stays_none_both_ways(self):
    field = JSONField()
    assert field.db_value(None) is None
    assert field.python_value(None) is None

  def test_text_is_stored_as_given(self):
    assert JSONField().db_value("[1, 2]") == "[1, 2]"


@pytest.mark.parametrize("field_type", [JSONField, EncryptedJSONField])
@pytest.mark.parametrize("value", ["", "{broken", "[1,]", "plain text", 12, True])
def test_json_fields_refuse_values_their_reader_cannot_decode(field_type, value):
  with pytest.raises(ValueError):
    field_type().db_value(value)


@pytest.fixture
def rows(tmp_path):
  storage = SqliteDatabase(tmp_path / "fields.db")

  class Payload(Model):
    text = EncryptedTextField(null=True)
    document = JSONField(null=True)
    encrypted_document = EncryptedJSONField(null=True)
    config = EncryptedKeyValueField(null=True)
    state = ChoiceField(("active", "inactive"), column_name="runtime_state", default="inactive")
    origins = OriginListField(null=True)
    name = AlphaNumericField(null=True, max_length=10)
    host = CleanCharField(null=True)
    label = LabelField(null=True)

    class Meta:
      database = storage

  storage.connect()
  try:
    storage.create_tables([Payload])
    yield Payload
  finally:
    storage.close()


def test_storage_fields_round_trip_through_sqlite(rows):
  text = "PASSWORD=pässwörd # keep this\nMSG=\"a # b\"\nEMPTY=\n"
  row = rows.create(text="private ✓", document={"nested": [1, None]}, encrypted_document={"token": "abc"}, config=text,
                    state="active", origins=[" HTTPS://APP.example.test:443/ ", "https://app.example.test"],
                    name="web1", host="worker-one", label="app-api")
  saved = rows.get_by_id(row.id)
  assert (saved.text, saved.document, saved.encrypted_document, saved.config, saved.state) == (
      "private ✓", {"nested": [1, None]}, {"token": "abc"}, text, "active")
  assert (saved.origins, saved.name, saved.host, saved.label) == (["https://app.example.test"], "web1", "worker-one", "app-api")
  raw = rows._meta.database.execute_sql("SELECT text, document, encrypted_document, config FROM payload").fetchone()
  assert raw[0] != "private ✓"
  assert json.loads(raw[1]) == {"nested": [1, None]}
  assert "token" not in raw[2]
  assert "PASSWORD" not in raw[3]


@pytest.mark.parametrize("writer", ["save", "update", "insert_many"])
def test_key_value_writes_preserve_text(rows, writer):
  text = "# note\nA=it's # comment\nB=\"x # y\"\n"
  row = rows.create()
  if writer == "save":
    row.config = text
    row.save()
  elif writer == "update":
    rows.update(config=text).where(rows.id == row.id).execute()
  else:
    rows.insert_many([{"id": row.id + 1, "config": text}]).execute()
    row.id += 1
  assert rows.get_by_id(row.id).config == text


@pytest.mark.parametrize("writer", ["save", "update", "insert_many"])
@pytest.mark.parametrize("value", ["A=1\nnot a pair", "A= leading space", 5, False])
def test_key_value_writes_refuse_invalid_text(rows, writer, value):
  row = rows.create(config="A=original")
  with pytest.raises(ValueError, match="payload.config"):
    if writer == "save":
      row.config = value
      row.save()
    elif writer == "update":
      rows.update(config=value).where(rows.id == row.id).execute()
    else:
      rows.insert_many([{"config": value}]).execute()
  assert rows.get_by_id(row.id).config == "A=original"
  assert rows.select().count() == 1


@pytest.mark.parametrize("value", [None, ""])
def test_empty_key_value_text_keeps_its_storage_meaning(rows, value):
  row = rows.create(config=value)
  assert rows.get_by_id(row.id).config == value


@pytest.mark.parametrize("field", ["text", "document", "encrypted_document", "config", "origins", "name", "host", "label"])
def test_nullable_storage_fields_preserve_null(rows, field):
  row = rows.create(**{field: None})
  assert getattr(rows.get_by_id(row.id), field) is None


@pytest.mark.parametrize("field,value", [
    ("name", "has-dashes"), ("name", "a" * 11), ("host", "Uppercase"), ("label", "-leading"),
    ("label", "a" * 64), ("origins", ["https://app.example.test/path"]),
])
@pytest.mark.parametrize("writer", ["save", "update", "insert_many"])
def test_scalar_rules_apply_to_all_orm_write_paths(rows, field, value, writer):
  row = rows.create()
  with pytest.raises(ValueError):
    if writer == "save":
      setattr(row, field, value)
      row.save()
    elif writer == "update":
      rows.update(**{field: value}).where(rows.id == row.id).execute()
    else:
      rows.insert_many([{field: value}]).execute()
  assert getattr(rows.get_by_id(row.id), field) is None
  assert rows.select().count() == 1


@pytest.mark.parametrize("writer", ["save", "update", "insert_many"])
def test_choices_are_validated_on_all_orm_write_paths(rows, writer):
  row = rows.create()
  with pytest.raises(ValueError, match="Invalid choice"):
    if writer == "save":
      row.state = "unknown"
      row.save()
    elif writer == "update":
      rows.update(state="unknown").where(rows.id == row.id).execute()
    else:
      rows.insert_many([{"state": "unknown"}]).execute()
  assert rows.get_by_id(row.id).state == "inactive"
  assert rows.select().count() == 1


@pytest.mark.parametrize("writer", ["update", "insert_many"])
def test_origin_lists_are_normalized_on_query_writes(rows, writer):
  origins = [" HTTPS://APP.example.test:443/ ", "https://app.example.test", "  "]
  if writer == "update":
    row = rows.create(origins=None)
    rows.update(origins=origins).where(rows.id == row.id).execute()
  else:
    rows.insert_many([{"origins": origins}]).execute()
  assert rows.get().origins == ["https://app.example.test"]


def test_sqlite_enforces_choices_without_the_field_codec(rows):
  row = rows.create()
  database = rows._meta.database
  database.execute_sql("UPDATE payload SET runtime_state = ? WHERE id = ?", ("active", row.id))
  assert rows.get_by_id(row.id).state == "active"
  for invalid in ("unknown", "Active", None):
    with pytest.raises(IntegrityError):
      database.execute_sql("UPDATE payload SET runtime_state = ? WHERE id = ?", (invalid, row.id))
  assert rows.get_by_id(row.id).state == "active"


def test_choice_constraints_preserve_other_constraints_and_quote_values(tmp_path):
  storage = SqliteDatabase(tmp_path / "choices.db")

  class Choice(Model):
    state = ChoiceField(["it's", "stopped"], null=True, column_name='state"name', constraints=[Check("length(extra) > 0")])
    extra = LabelField()

    class Meta:
      database = storage

  storage.connect()
  try:
    storage.create_tables([Choice])
    Choice.create(state="it's", extra="label")
    Choice.create(state=None, extra="null")
    assert [row.state for row in Choice.select().order_by(Choice.id)] == ["it's", None]
    storage.execute_sql('INSERT INTO choice ("state""name", extra) VALUES (?, ?)', ("stopped", "raw"))
    with pytest.raises(IntegrityError):
      storage.execute_sql('INSERT INTO choice ("state""name", extra) VALUES (?, ?)', ("other", "raw"))
    with pytest.raises(IntegrityError):
      storage.execute_sql('INSERT INTO choice ("state""name", extra) VALUES (?, ?)', ("stopped", ""))
  finally:
    storage.close()


@pytest.mark.parametrize("choices", [[], ["x", "x"], [1], ["x", None], "active"])
def test_choice_definitions_require_distinct_strings(choices):
  with pytest.raises(ValueError, match="Choices must"):
    ChoiceField(choices)


def test_choice_definition_is_frozen_from_the_callers_list():
  choices = ["active", "inactive"]
  field = ChoiceField(choices)
  choices.append("unknown")
  with pytest.raises(ValueError, match="Invalid choice"):
    field.db_value("unknown")


@pytest.mark.parametrize("key", [None, "", "not-a-key", "short"])
def test_explicit_encryption_initialization_refuses_invalid_keys(key, monkeypatch):
  monkeypatch.setattr(encryption, "_cipher", None)
  with pytest.raises(ValueError, match="ENCRYPTION_KEY must be a key made with"):
    encryption.initialize_encryption(key)
  with pytest.raises(RuntimeError, match="has not been initialized"):
    EncryptedTextField().db_value("text")


def test_encrypted_storage_requires_explicit_initialization(monkeypatch):
  monkeypatch.setattr(encryption, "_cipher", None)
  for field, value in ((EncryptedTextField(), "text"), (EncryptedJSONField(), {}), (EncryptedKeyValueField(), "A=1")):
    assert field.db_value(None) is None
    assert field.python_value(None) is None
    with pytest.raises(RuntimeError, match="has not been initialized"):
      field.db_value(value)
    with pytest.raises(RuntimeError, match="has not been initialized"):
      field.python_value("ciphertext")


def test_initialization_uses_its_explicit_key_and_wrong_key_data_is_refused(monkeypatch):
  key = Fernet.generate_key().decode()
  monkeypatch.setenv("ENCRYPTION_KEY", "invalid-environment-value")
  encryption.initialize_encryption(key)
  field = EncryptedTextField()
  stored = field.db_value("original data")
  assert Fernet(key).decrypt(stored.encode()) == b"original data"
  encryption.initialize_encryption(Fernet.generate_key().decode())
  with pytest.raises(InvalidToken):
    field.python_value(stored)


@pytest.mark.parametrize("key", [None, "", "not-a-key"])
def test_foundation_imports_need_no_runtime_configuration_or_models(key, tmp_path):
  env = {name: value for name, value in os.environ.items()
         if name not in {"ENCRYPTION_KEY", "DOMAIN", "TS_NAME", "CLOUDFLARE_API_TOKEN"}}
  if key is not None:
    env["ENCRYPTION_KEY"] = key
  code = f"""
import sys
sys.path.insert(0, {str(Path(__file__).parents[3])!r})
import services.db.fields
import values.names
import values.key_values
assert not {{'config', 'services.db.models', 'services.db.signals', 'services.manager'}} & sys.modules.keys()
"""
  result = subprocess.run([sys.executable, "-I", "-c", code], env=env, cwd=tmp_path, capture_output=True, text=True)
  assert result.returncode == 0, result.stderr
  assert result.stdout == result.stderr == ""
  assert list(tmp_path.iterdir()) == []
