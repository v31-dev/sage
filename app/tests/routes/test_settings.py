import pytest

from services.logs import Logs
from services.settings import Settings

FILE = "sage-debug-2026-01-01T12-00-00Z.log"


@pytest.fixture(autouse=True)
def settings(db):
  """Loaded at start, which makes their rows."""
  return Settings()


class TestDebugCapture:
  def test_a_stop_time_starts_a_capture_and_queues_no_check(self, client, manager, clock):
    response = client.put("/api/settings/debug", json={"value": {"stop_at": "2026-01-01T13:00:00.000Z"}})

    assert response.status_code == 200
    assert response.json()["value"] == {"stop_at": "2026-01-01T13:00:00"}
    assert Logs().debug_file == FILE
    assert manager.tasks == []

  def test_clearing_the_stop_time_stops_the_capture(self, client, clock):
    client.put("/api/settings/debug", json={"value": {"stop_at": "2026-01-01T13:00:00Z"}})

    response = client.put("/api/settings/debug", json={"value": {"stop_at": None}})

    assert response.json()["value"] == {"stop_at": None}
    assert Logs().debug_file is None

  def test_a_stop_time_that_has_passed_is_refused(self, client, clock):
    response = client.put("/api/settings/debug", json={"value": {"stop_at": "2026-01-01T11:00:00Z"}})

    assert response.status_code == 400
    assert response.json() == {"detail": "The stop time has passed."}

  def test_the_capture_is_its_stop_time_and_its_files_with_the_one_being_written(self, client, clock):
    (Logs().debug_dir / "sage-debug-2025-12-31T00-00-00Z.log").write_text("a line\n")
    client.put("/api/settings/debug", json={"value": {"stop_at": "2026-01-01T13:00:00Z"}})

    capture = client.get("/api/settings/debug/capture").json()

    assert capture["stop_at"] == "2026-01-01T13:00:00"
    assert [(file["name"], file["size"], file["active"]) for file in capture["files"]] == [
        (FILE, 0, True), ("sage-debug-2025-12-31T00-00-00Z.log", 7, False)]

  def test_a_file_downloads_as_an_attachment(self, client, db):
    (Logs().debug_dir / FILE).write_text("one\ntwo\n")

    response = client.get(f"/api/settings/debug/files/{FILE}")

    assert response.content == b"one\ntwo\n"
    assert response.headers["content-disposition"] == f'attachment; filename="{FILE}"'

  def test_a_file_is_deleted(self, client, db):
    (Logs().debug_dir / FILE).write_text("one\n")

    assert client.delete(f"/api/settings/debug/files/{FILE}").status_code == 200
    assert not (Logs().debug_dir / FILE).exists()

  @pytest.mark.parametrize("method", ["get", "delete"])
  @pytest.mark.parametrize("name, status", [(FILE, 404), ("data.db", 400)], ids=["missing", "not a capture file"])
  def test_a_missing_file_or_a_name_that_is_not_one_is_refused(self, client, db, method, name, status):
    assert client.request(method.upper(), f"/api/settings/debug/files/{name}").status_code == status
