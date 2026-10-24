import pytest
import yaml

from utils.common import dict_to_obj, format_yaml, get_env, parse_multiline_kv, strip_env_comment


def pairs(config, **kwargs):
  return parse_multiline_kv(config, lambda key, value: (key, value), **kwargs)


class TestStripEnvComment:
  """The rules are Docker Compose's, which reads the same text from the .env file on the worker."""

  @pytest.mark.parametrize("line, kept", [
      ("", ""),
      ("# the whole line", ""),
      ("   # indented", ""),
      ("KEY=VAL # comment", "KEY=VAL "),
      ("KEY=1 # a = b", "KEY=1 "),
      ("KEY= # no value", "KEY= "),
      ('KEY="VAL" # comment', 'KEY="VAL" '),
      ('KEY="VAL"# comment', 'KEY="VAL"'),
      ('KEY= "a # b" # c', 'KEY= "a # b" '),
      ("KEY='a # b' # c", "KEY='a # b' "),
      ("KEY=don't stop # note", "KEY=don't stop "),
      ('KEY=5" pipe # note', 'KEY=5" pipe '),
      ("KEY='Let\\'s go!' # c", "KEY='Let\\'s go!' "),
      ('KEY="a\\"b # still quoted" # c', 'KEY="a\\"b # still quoted" '),
  ])
  def test_cuts_the_comment(self, line, kept):
    assert strip_env_comment(line) == kept

  @pytest.mark.parametrize("line", [
      "KEY=VAL#not a comment",
      "KEY=pa#ss",
      "KEY=#starts with a hash",
      'KEY="VAL # not a comment"',
      "KEY='a # b'",
      "KEY='unterminated # kept",
      'KEY="a\\"b # still quoted"',
      "NOT A PAIR # why",
      "A#B=1",
      "KEY=",
      "KEY=plain",
  ])
  def test_leaves_these_lines_whole(self, line):
    assert strip_env_comment(line) == line


class TestParseMultilineKv:
  def test_returns_what_the_function_makes_of_each_pair(self):
    assert parse_multiline_kv("A=1\nB=2", lambda key, value: f"{key}:{value}") == ["A:1", "B:2"]

  @pytest.mark.parametrize("config", [None, "", "\n\n"])
  def test_empty_input_gives_nothing(self, config):
    assert pairs(config) == []

  def test_skips_blank_comment_only_and_valueless_lines(self):
    assert pairs("\n# a comment\n   # indented comment\nNOT_A_PAIR\nA=1\n") == [("A", "1")]

  def test_splits_on_the_first_equals_and_strips_whitespace(self):
    assert pairs("  KEY  =  a=b=c  ") == [("KEY", "a=b=c")]

  def test_empty_value_is_kept(self):
    assert pairs("KEY=") == [("KEY", "")]

  def test_handles_windows_line_endings(self):
    assert pairs("A=1\r\nB=2\r\n") == [("A", "1"), ("B", "2")]

  def test_strips_an_inline_comment(self):
    assert pairs("KEY=value # why") == [("KEY", "value")]

  @pytest.mark.parametrize("line, value", [
      ("VAR=VAL # comment", "VAL"),
      ("VAR=VAL# not a comment", "VAL# not a comment"),
      ('VAR="VAL" # comment', '"VAL"'),
      ("VAR = VAL  ", "VAL"),
      ("PASSWORD=pa#ss", "pa#ss"),
      ("PASSWORD=pa#ss # a note", "pa#ss"),
      ("MSG=don't stop # a note", "don't stop"),
      ('SIZE=5" pipe # a note', '5" pipe'),
  ])
  def test_follows_the_documented_compose_examples(self, line, value):
    assert pairs(line)[0][1] == value

  def test_without_a_closing_quote_the_rest_of_the_line_is_the_value(self):
    assert pairs("A='x # y") == [("A", "'x # y")]

  def test_an_escaped_quote_does_not_end_a_quoted_value(self):
    assert pairs("A='Let\\'s go! # still the value' # note", strip_quotes=True) == [("A", "Let\\'s go! # still the value")]

  @pytest.mark.parametrize("line, value", [
      ('KEY="a # b"', '"a # b"'),
      ("KEY='a # b'", "'a # b'"),
      ('KEY="a # b" # real comment', '"a # b"'),
  ])
  def test_a_hash_inside_quotes_is_not_a_comment(self, line, value):
    assert pairs(line) == [("KEY", value)]

  @pytest.mark.parametrize("value, stripped", [
      ('"two words"', "two words"),
      ("'two words'", "two words"),
      ('"it\'s"', "it's"),
      ('""', ""),
  ])
  def test_strip_quotes_removes_one_matching_pair(self, value, stripped):
    assert pairs(f"KEY={value}", strip_quotes=True) == [("KEY", stripped)]

  @pytest.mark.parametrize("value", ["\"abc'", "'abc\"", '"', "abc"])
  def test_strip_quotes_leaves_anything_else_alone(self, value):
    assert pairs(f"KEY={value}", strip_quotes=True) == [("KEY", value)]

  def test_strip_quotes_removes_only_the_outer_pair(self):
    assert pairs('KEY=""x""', strip_quotes=True) == [("KEY", '"x"')]

  def test_quotes_are_kept_without_strip_quotes(self):
    assert pairs('KEY="two words"') == [("KEY", '"two words"')]


class TestFormatYaml:
  def test_keeps_key_order_and_drops_comments(self):
    assert format_yaml("b: 1\na: 2  # note\n") == "b: 1\na: 2\n"

  def test_writes_flow_style_as_block_style(self):
    assert format_yaml("a: {b: 1, c: [1, 2]}") == "a:\n  b: 1\n  c:\n  - 1\n  - 2\n"

  def test_output_loads_back_to_the_same_data(self):
    content = 'image: "nginx:1"\ncommand: ["sh", "-c", "echo hi"]\nenv:\n  A: "1"\n'
    assert yaml.safe_load(format_yaml(content)) == yaml.safe_load(content)

  def test_is_idempotent(self):
    once = format_yaml("a: {b: 1}\nc: [x, y]\n")
    assert format_yaml(once) == once

  def test_invalid_yaml_raises(self):
    with pytest.raises(yaml.YAMLError):
      format_yaml("a: [unclosed")


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


def test_dict_to_obj_gives_attribute_access():
  obj = dict_to_obj({"hostname": "w1", "online": True})
  assert (obj.hostname, obj.online) == ("w1", True)
