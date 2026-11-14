import json

from peewee import CharField, Check, NodeList, TextField

from values.key_values import validate_multiline_kv
from values.names import normalize_origins, validate_alphanumeric_name, validate_label, validate_name

from .encryption import decrypt, encrypt


class EncryptedTextField(TextField):
  def python_value(self, value):
    return decrypt(value)

  def db_value(self, value):
    return encrypt(value)


class EncryptedKeyValueField(EncryptedTextField):
  def db_value(self, value):
    field_name = f"{self.model._meta.table_name}.{self.name}" if hasattr(self, "model") else "config"
    validate_multiline_kv(value, field_name)
    return super().db_value(value)


class JSONField(TextField):
  def python_value(self, value):
    return None if value is None else json.loads(value)

  def db_value(self, value):
    if value is None:
      return None
    if isinstance(value, (dict, list)):
      return json.dumps(value)
    if not isinstance(value, str):
      raise ValueError("JSON value must be a dict, list, or JSON text.")
    json.loads(value)
    return value


class EncryptedJSONField(JSONField):
  def python_value(self, value):
    return super().python_value(decrypt(value))

  def db_value(self, value):
    return encrypt(super().db_value(value))


class OriginListField(JSONField):
  validate = staticmethod(normalize_origins)

  def db_value(self, value):
    return super().db_value(self.validate(value))


class CleanCharField(CharField):
  validate = staticmethod(validate_name)

  def db_value(self, value):
    return self.validate(value)


class AlphaNumericField(CharField):
  def validate(self, value):
    return validate_alphanumeric_name(value, self.max_length)

  def db_value(self, value):
    return self.validate(value)


class LabelField(CharField):
  validate = staticmethod(validate_label)

  def __init__(self, *args, **kwargs):
    super().__init__(*args, max_length=63, **kwargs)

  def db_value(self, value):
    return self.validate(value)


class ChoiceField(CharField):
  def __init__(self, choices, *args, **kwargs):
    if isinstance(choices, str):
      raise ValueError("Choices must be a nonempty collection of distinct strings.")
    choices = tuple(choices)
    if not choices or any(not isinstance(value, str) for value in choices) or len(set(choices)) != len(choices):
      raise ValueError("Choices must be a nonempty collection of distinct strings.")
    super().__init__(*args, choices=choices, **kwargs)

  def db_value(self, value):
    if value is not None and value not in self.choices:
      raise ValueError(f"Invalid choice {value!r}: expected one of {', '.join(self.choices)}.")
    return value

  def ddl(self, ctx):
    column = '"' + self.column_name.replace('"', '""') + '"'
    choices = ", ".join("'" + value.replace("'", "''") + "'" for value in self.choices)
    return NodeList((super().ddl(ctx), Check(f"{column} IN ({choices})")))
