import pytest

from values.key_values import parse_multiline_kv, strip_env_comment, validate_multiline_kv


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
