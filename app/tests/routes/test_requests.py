import json

import pytest

from services.logs import Logs

URL = "/api/requests/"


def access(path, start, status=200, duration_ms=1):
  return json.dumps({"container_name": "traefik", "timestamp": start, "stream": "stdout", "message": json.dumps({
      "StartUTC": start, "RequestHost": "web.example.com", "RequestMethod": "GET", "RequestPath": path,
      "DownstreamStatus": status, "Duration": duration_ms * 1_000_000})})


@pytest.fixture
def stored(db):
  Logs().ingest("w1", "\n".join([access("/ok", "2026-01-01T12:00:00Z"),
                                 access("/missing", "2026-01-01T12:30:00.5Z", status=404, duration_ms=300),
                                 access("/broken", "2026-01-01T13:00:00Z", status=502)]).encode())


def paths(response):
  return [row["path"] for row in response.json()["requests"]]


class TestRequests:
  def test_they_come_newest_first_with_whether_there_were_more(self, client, stored):
    response = client.get(URL)

    assert response.status_code == 200
    assert paths(response) == ["/broken", "/missing", "/ok"]
    assert response.json()["more"] is False

  @pytest.mark.parametrize("params, found", [
      ({"status": "4xx"}, ["/missing"]),
      ({"status": "5xx"}, ["/broken"]),
      ({"min_duration_ms": 300}, ["/missing"]),
      ({"path": "miss", "worker": "w1", "method": "GET", "host": "WEB"}, ["/missing"]),
      ({"from_ts": "2026-01-01T12:30:00.500Z", "to_ts": "2026-01-01T13:00:00Z"}, ["/missing"]),
      ({"from_ts": "2026-01-01T18:00:00.5+05:30"}, ["/broken", "/missing"]),
  ])
  def test_the_filters_reach_the_query_with_times_in_any_zone(self, client, stored, params, found):
    assert paths(client.get(URL, params=params)) == found

  @pytest.mark.parametrize("params", [{"status": "6xx"}, {"status": "404"}, {"min_duration_ms": -1}])
  def test_a_filter_that_cannot_match_anything_is_refused(self, client, db, params):
    assert client.get(URL, params=params).status_code == 422
