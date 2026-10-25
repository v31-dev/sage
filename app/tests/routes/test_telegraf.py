import gzip
import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
from services.metrics import Metrics
from services.reports import Reports, parse_batch

BATCH = json.loads((Path(__file__).parent.parent / "data" / "telegraf_batch.json").read_text())
URL = "/api/telegraf/report"


@pytest.fixture
def client(db, thread_connections):
  from api_ingest import app
  return TestClient(app)


def post(client, body, node="w1", encoding="gzip", chunked=True):
  """What Telegraf sends: a gzip body, chunked (no Content-Length)."""
  headers = {"X-Node-Name": node, "Content-Type": "application/json"}
  if encoding:
    headers["Content-Encoding"] = encoding
    body = gzip.compress(body)
  content = iter([body[:40], body[40:]]) if chunked else body
  return client.post(URL, content=content, headers=headers)


class TestReport:
  def test_a_chunked_gzip_report_from_a_worker_is_kept(self, client, worker):
    response = post(client, json.dumps(BATCH).encode())
    assert response.status_code == 204
    assert Reports().latest("w1").snapshot.containers["web"].status == "running"

  def test_a_plain_body_with_a_length_is_read_too(self, client, worker):
    response = post(client, json.dumps(BATCH).encode(), encoding=None, chunked=False)
    assert response.status_code == 204
    assert Reports().latest("w1") is not None

  def test_a_batch_of_several_gathers_keeps_the_newest(self, client, worker):
    post(client, json.dumps(BATCH).encode())
    assert Reports().latest("w1").snapshot.at == parse_batch(BATCH)[-1].at

  def test_the_snapshots_go_to_the_metrics_under_the_workers_name(self, client, worker, register):
    class FakeMetrics:
      def __init__(self):
        self.ingested = []

      def ingest(self, hostname, snapshots):
        self.ingested.append((hostname, snapshots))

    metrics = register(Metrics, FakeMetrics())
    post(client, json.dumps(BATCH).encode())
    assert metrics.ingested == [("w1", parse_batch(BATCH))]

  def test_a_refused_report_reaches_neither_the_store_nor_the_metrics(self, client, worker, register):
    class FakeMetrics:
      def ingest(self, hostname, snapshots):
        raise AssertionError("ingested")

    register(Metrics, FakeMetrics())
    assert post(client, b"not json").status_code == 400
    assert post(client, json.dumps(BATCH).encode(), node="stranger").status_code == 404

  def test_the_first_report_is_logged_and_the_next_is_not(self, client, worker, caplog):
    with caplog.at_level(logging.INFO, logger="routes.telegraf"):
      post(client, json.dumps(BATCH).encode())
      post(client, json.dumps(BATCH).encode())
    assert [r.message for r in caplog.records] == ["Receiving reports from w1."]

  @pytest.mark.parametrize("node", ["", "stranger", "Not A Name"])
  def test_a_name_that_is_not_a_worker_is_refused_and_kept_out(self, client, worker, node):
    response = post(client, json.dumps(BATCH).encode(), node=node)
    assert response.status_code == 404
    assert Reports().latest(node) is None

  def test_an_unknown_name_is_logged_once(self, client, worker, caplog):
    with caplog.at_level(logging.WARNING, logger="routes.telegraf"):
      post(client, b"{}", node="stranger")
      post(client, b"{}", node="stranger")
    assert len(caplog.records) == 1

  def test_a_worker_that_was_removed_is_refused(self, client, worker):
    worker.delete_instance()
    assert post(client, json.dumps(BATCH).encode()).status_code == 404

  @pytest.mark.parametrize("body", [b"not json", b"[]", b'{"metrics": 5}', b'{"metrics": [{"name": "cpu"}]}'])
  def test_a_body_that_is_not_telegrafs_json_is_a_400_and_changes_nothing(self, client, worker, body):
    assert post(client, body).status_code == 400
    assert Reports().latest("w1") is None

  def test_a_gzip_header_over_a_body_that_is_not_gzip_is_a_400(self, client, worker):
    response = client.post(URL, content=b"plainly not gzip", headers={"X-Node-Name": "w1", "Content-Encoding": "gzip"})
    assert response.status_code == 400

  def test_a_body_over_the_limit_is_refused_before_it_is_decompressed(self, client, worker, monkeypatch):
    monkeypatch.setattr(config, "REPORT_MAX_BYTES", 100)
    response = client.post(URL, content=b"x" * 101, headers={"X-Node-Name": "w1"})
    assert response.status_code == 413

  def test_a_small_body_that_inflates_over_the_limit_is_refused(self, client, worker, monkeypatch):
    monkeypatch.setattr(config, "REPORT_MAX_BYTES", 1000)
    bomb = gzip.compress(b"0" * 100_000)
    assert len(bomb) < 1000
    response = client.post(URL, content=bomb, headers={"X-Node-Name": "w1", "Content-Encoding": "gzip"})
    assert response.status_code == 413
    assert Reports().latest("w1") is None
