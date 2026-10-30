import json
import threading

import pytest
from fastapi.testclient import TestClient

import config
from services.logs import Logs

URL = "/api/vector/logs"


@pytest.fixture
def client(db, thread_connections):
  from api_ingest import app
  return TestClient(app)


def body(*messages, container="demo-web"):
  return "\n".join(json.dumps({"container_name": container, "timestamp": "2026-01-01T12:00:00.000000000Z",
                               "stream": "stdout", "message": message}) for message in messages).encode()


def stored(container="demo-web"):
  return [row["message"] for row in Logs().query_logs(container)["lines"]]


class TestIntake:
  def test_a_push_is_stored_and_answered_with_no_content(self, client):
    response = client.post(URL, content=body("one", "two"), headers={"X-Node-Name": "w1"})

    assert response.status_code == 204
    assert stored() == ["one", "two"]

  def test_it_is_decoded_and_written_on_the_logs_thread_not_the_event_loop(self, client, monkeypatch):
    threads = []
    ingest = Logs.ingest

    def watched(self, hostname, pushed):
      threads.append(threading.current_thread().name)
      return ingest(self, hostname, pushed)

    monkeypatch.setattr(Logs, "ingest", watched)
    client.post(URL, content=body("one"), headers={"X-Node-Name": "w1"})

    assert [name.startswith("sage-logs") for name in threads] == [True]

  def test_lines_it_skips_are_counted_in_a_warning(self, client, caplog):
    response = client.post(URL, content=b"not json\n" + body("kept"), headers={"X-Node-Name": "w1"})

    assert response.status_code == 204
    assert stored() == ["kept"]
    assert "Skipped 1 log lines from w1" in caplog.text

  def test_a_push_without_the_workers_name_is_refused(self, client):
    response = client.post(URL, content=body("one"))

    assert response.status_code == 400
    assert stored() == []

  def test_a_push_over_the_limit_is_refused(self, client, monkeypatch):
    monkeypatch.setattr(config, "LOG_INTAKE_MAX_BYTES", 100)

    response = client.post(URL, content=body("x" * 200), headers={"X-Node-Name": "w1"})

    assert response.status_code == 413
    assert stored() == []

  def test_a_write_that_fails_is_a_5xx_so_that_vector_sends_the_batch_again(self, client):
    (config.DATA_DIR / "logs").mkdir(exist_ok=True)
    (config.DATA_DIR / "logs" / "demo-web.db").mkdir()

    response = client.post(URL, content=body("lost") + b"\n" + body("kept", container="demo-api"),
                           headers={"X-Node-Name": "w1"})

    assert response.status_code == 503
    assert stored("demo-api") == ["kept"]
