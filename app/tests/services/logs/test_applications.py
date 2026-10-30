import sqlite3

import pytest

from services.updates import UpdateFeed

from .builders import lines, moment, stamp


class TestStoring:
  def test_lines_come_back_oldest_first_and_can_be_filtered(self, logs):
    logs.write_application_lines("web", lines(0, 5, host="w1") + lines(5, 5, host="w2"))
    assert [row["ts"] for row in logs.query_application_lines("web")["lines"]] == [stamp(i) for i in range(10)]
    assert {row["hostname"] for row in logs.query_application_lines("web", hostname="w2")["lines"]} == {"w2"}
    assert [row["ts"] for row in logs.query_application_lines("web", from_ts=moment(2), to_ts=moment(4))["lines"]] == [stamp(2), stamp(3)]

  def test_the_message_is_searchable(self, logs):
    logs.write_application_lines("web", lines(0, 20))
    assert [row["ts"] for row in logs.query_application_lines("web", search="line7")["lines"]] == [stamp(7)]

  def test_a_query_gives_the_newest_lines_asked_for_and_says_when_there_were_more(self, logs):
    logs.write_application_lines("web", lines(0, 5))
    assert logs.query_application_lines("web", limit=5)["more"] is False
    logs.write_application_lines("web", lines(5, 2))
    for search in ("", "line"):
      result = logs.query_application_lines("web", search=search, limit=5)
      assert [row["ts"] for row in result["lines"]] == [stamp(index) for index in range(2, 7)]
      assert result["more"] is True


def messages(*texts):
  return [{"hostname": "w1", "ts": stamp(index), "stream": "stdout", "message": text} for index, text in enumerate(texts)]


SAID = ["GET /items/42 a-b", 'he said "hi"', "100% done", "snake_case value", "UPPER Case", "plain"]


class TestSearch:
  @pytest.mark.parametrize("search, found", [
      ("ITEMS/4", ["GET /items/42 a-b"]),
      ("a-b", ["GET /items/42 a-b"]),
      ('"hi"', ['he said "hi"']),
      ("%", ["100% done"]),
      ("_", ["snake_case value"]),
      ("upper c", ["UPPER Case"]),
      ("ak", ["snake_case value"]),
      ("nowhere", []),
  ])
  def test_it_finds_the_text_anywhere_in_a_line_in_any_case_whatever_it_holds(self, logs, search, found):
    logs.write_application_lines("web", messages(*SAID))
    assert [row["message"] for row in logs.query_application_lines("web", search=search)["lines"]] == found

  def test_it_goes_with_the_worker_and_the_window(self, logs):
    logs.write_application_lines("web", lines(0, 5, host="w1", word="hit") + lines(5, 5, host="w2", word="hit"))
    result = logs.query_application_lines("web", search="hit", hostname="w2", from_ts=moment(6), to_ts=moment(8))
    assert [row["ts"] for row in result["lines"]] == [stamp(6), stamp(7)]

  def test_a_shard_indexed_before_the_trigram_tokenizer_is_searched_the_same_way(self, logs):
    db = logs.application_store("web").db
    db.execute_sql("DROP TABLE containerlogsindex")
    db.execute_sql("CREATE VIRTUAL TABLE containerlogsindex USING fts5(message, content=containerlogs, content_rowid=id)")
    logs.write_application_lines("web", messages(*SAID))
    assert "trigram" not in db.execute_sql("SELECT sql FROM sqlite_master WHERE name = 'containerlogsindex'").fetchone()[0]
    for search, found in [("ITEMS/4", ["GET /items/42 a-b"]), ("%", ["100% done"]), ("ak", ["snake_case value"])]:
      assert [row["message"] for row in logs.query_application_lines("web", search=search)["lines"]] == found

  @pytest.mark.parametrize("name", ["", "../etc", "Web", "a b", "a/b", "tasks"])
  def test_an_application_name_that_could_leave_the_logs_directory_or_is_the_task_logs_is_refused(self, logs, name):
    with pytest.raises(ValueError):
      logs.application_store(name)

  def test_the_timestamp_is_indexed(self, logs):
    shard = logs.application_store("web")
    indexes = [row[1] for row in shard.db.execute_sql("PRAGMA index_list('containerlogs')").fetchall()]
    assert "containerlogs_ts" in indexes

  def test_a_shard_made_before_the_index_gets_it_when_opened(self, logs):
    path = logs.db_path / "logs" / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE containerlogs (id INTEGER PRIMARY KEY AUTOINCREMENT, hostname VARCHAR(255) NOT NULL, "
                "ts VARCHAR(255) NOT NULL, stream VARCHAR(255) NOT NULL, message TEXT NOT NULL)")
    old.commit()
    old.close()
    shard = logs.application_store("old")
    indexes = [row[1] for row in shard.db.execute_sql("PRAGMA index_list('containerlogs')").fetchall()]
    assert "containerlogs_ts" in indexes


class TestAfter:
  """What a view loads when the socket connects again: the rows written after the last one it holds."""

  def test_an_applications_lines(self, logs):
    logs.write_application_lines("web", lines(0, 5))
    held = logs.query_application_lines("web")["lines"][2]["id"]
    assert [row["ts"] for row in logs.query_application_lines("web", after_id=held)["lines"]] == [stamp(3), stamp(4)]
    assert [row["ts"] for row in logs.query_application_lines("web", search="line", after_id=held)["lines"]] == [stamp(3), stamp(4)]


class TestMarkers:
  def test_a_marker_is_a_line_of_its_own_from_the_worker_at_the_managers_time_and_pushed(self, logs, clock, published):
    logs.write_marker("demo-web", "w2", "Deployed (task t1)")

    [line] = logs.query_application_lines("demo-web")["lines"]
    assert {key: line[key] for key in ("hostname", "ts", "stream", "message")} == {
        "hostname": "w2", "ts": "2026-01-01T12:00:00.000000000Z", "stream": "sage", "message": "Deployed (task t1)"}
    assert [(feed, key) for feed, _, key in published] == [(UpdateFeed.APPLICATION_LOGS, "demo-web")]
