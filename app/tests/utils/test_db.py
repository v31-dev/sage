import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken

from utils.common import parse_multiline_kv
from utils.db import AlphaNumericField, CleanCharField, EncryptedJSONField, EncryptedTextField, JSONField, validate_multiline_kv


class TestValidateMultilineKv:
  @pytest.mark.parametrize("config", [
      None, "", "\n", "# only a comment", "A=1", "A=1\nB_2=two words", "A=\nB=x # inline comment", "\n\nA=1\n\n",
      "A=don't stop # why", "A=abc#def", 'A="a # b" # c', "A=1 # a = b",
  ])
  def test_accepts_valid_input(self, config):
    validate_multiline_kv(config)

  @pytest.mark.parametrize("config", ["NOT A PAIR # why = x", "A=1\nnot a pair # a = b", "A#B=1", "KEY= value # c"])
  def test_a_comment_does_not_make_a_malformed_line_valid(self, config):
    with pytest.raises(ValueError, match="invalid line"):
      validate_multiline_kv(config)

  def test_accepts_what_the_parser_reads(self):
    config = "A=1 # c\nB=\"x # y\"\nC=p#q\nD=it's # c\n"
    validate_multiline_kv(config)
    assert parse_multiline_kv(config, lambda key, value: (key, value)) == [("A", "1"), ("B", '"x # y"'), ("C", "p#q"), ("D", "it's")]

  @pytest.mark.parametrize("config, line", [
      ("A=1\nnot a pair", 2),
      ("A", 1),
      ("1KEY=a", 1),
      ("KEY-NAME=a", 1),
      (" KEY=a", 1),
      ("KEY =a", 1),
      ("A=1\nB=2\nKEY= leading space", 3),
  ])
  def test_rejects_a_malformed_line_and_names_it(self, config, line):
    with pytest.raises(ValueError, match=f"invalid line {line}"):
      validate_multiline_kv(config)

  def test_the_error_names_the_field(self):
    with pytest.raises(ValueError, match="application.env must contain"):
      validate_multiline_kv("oops", "application.env")


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
    assert AlphaNumericField.validate(value) == value

  def test_validate_lets_none_through(self):
    assert AlphaNumericField.validate(None) is None

  @pytest.mark.parametrize("value", ["1abc", "Abc", "a-b", "a_b", "a b", ""])
  def test_validate_rejects_anything_else(self, value):
    with pytest.raises(ValueError, match="Invalid name"):
      AlphaNumericField.validate(value)

  def test_db_value_validates(self):
    with pytest.raises(ValueError):
      AlphaNumericField().db_value("not valid")

  @pytest.mark.parametrize("label, name", [
      ("My App", "myapp"),
      ("My App 2!", "myapp2"),
      ("  Web-Server_01 ", "webserver01"),
      ("a", "a"),
  ])
  def test_clean_derives_a_name_from_a_label(self, label, name):
    assert AlphaNumericField.clean(label) == name

  @pytest.mark.parametrize("label", ["", None, "123", "1app", "!!!", "   "])
  def test_clean_refuses_a_label_with_no_valid_name(self, label):
    with pytest.raises(ValueError, match="does not produce a valid name"):
      AlphaNumericField.clean(label)


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


class TestEncryptionKeyAtImport:
  def run(self, key):
    env = {name: value for name, value in os.environ.items() if name != "ENCRYPTION_KEY"}
    if key is not None:
      env["ENCRYPTION_KEY"] = key
    return subprocess.run([sys.executable, "-c", "import utils.db"], env=env, cwd=Path(__file__).parents[2], capture_output=True, text=True)

  @pytest.mark.parametrize("key", [None, "", "not-a-key", "short"])
  def test_a_missing_or_invalid_key_stops_the_import_with_a_critical_line(self, key):
    result = self.run(key)
    assert result.returncode == 1
    assert "[CRITICAL]" in result.stderr
    assert "ENCRYPTION_KEY must be a key made with: openssl rand -base64 32" in result.stderr

  def test_a_valid_key_imports(self):
    assert self.run(Fernet.generate_key().decode()).returncode == 0


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
