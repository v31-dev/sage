from fastapi.testclient import TestClient

from api_health import app


def client():
  return TestClient(app)


class TestHealth:
  def test_the_probe_is_answered(self):
    response = client().get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

  def test_nothing_else_is_served(self):
    assert client().get("/").status_code == 404
    assert client().get("/api").status_code == 404
    assert client().post("/health").status_code == 405

  def test_a_browser_is_refused(self):
    assert client().get("/health", headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate",
                                            "Sec-Fetch-Dest": "document"}).status_code == 403
