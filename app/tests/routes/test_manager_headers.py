import pytest

HEADERS = ["Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options", "Strict-Transport-Security",
           "Referrer-Policy", "Permissions-Policy"]


def directives(policy):
  return {part.split()[0]: part.split()[1:] for part in (piece.strip() for piece in policy.split(";")) if part}


class TestTheManagersHeaders:
  @pytest.mark.parametrize("method, path", [("GET", "/"), ("GET", "/api/nothing-here"), ("POST", "/api/nothing-here")],
                           ids=["a-page", "not-found", "not-allowed"])
  def test_every_answer_carries_them(self, client, method, path):
    response = client.request(method, path)
    assert all(name in response.headers for name in HEADERS)

  def test_a_change_refused_while_paused_carries_them_too(self, client, manager):
    manager.state = "paused"
    response = client.post("/api/settings/s3/test", json={})
    assert response.status_code == 503
    assert all(name in response.headers for name in HEADERS)

  def test_scripts_come_from_the_manager_only_and_no_other_page_frames_it(self, client):
    response = client.get("/")
    policy = directives(response.headers["Content-Security-Policy"])
    assert policy["script-src"] == ["'self'"] and policy["default-src"] == ["'self'"]
    assert policy["connect-src"] == ["'self'"] and policy["frame-ancestors"] == ["'none'"]
    assert response.headers["X-Frame-Options"] == "DENY"
