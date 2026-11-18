import httpx
import pytest

import config
from services.releases import Releases
from services.updates import UpdateSource

RELEASE = {"tag_name": "v1.2.3", "name": "Sage 1.2.3", "body": "<!-- for the editor -->\n## Changes\n\nFaster.\n",
           "html_url": "https://github.com/v31-dev/sage/releases/tag/v1.2.3", "published_at": "2026-11-01T10:00:00Z"}


@pytest.fixture
def github(monkeypatch, clock):
  """GitHub's latest release endpoint answering each request with the next of `answers` (the last one repeats)."""
  monkeypatch.setattr(config, "VERSION", "1.0.0")
  answers = []

  def handle(request):
    assert str(request.url) == config.LATEST_RELEASE_URL
    answer = answers.pop(0) if len(answers) > 1 else answers[0]
    if isinstance(answer, Exception):
      raise answer
    return answer

  releases = Releases()
  releases.http.close()
  releases.http = httpx.Client(transport=httpx.MockTransport(handle))
  yield releases, answers
  releases.http.close()


def answer(**fields):
  return httpx.Response(200, json={**RELEASE, **fields})


class TestReleases:
  def test_before_a_fetch_only_the_running_version_is_known(self, github):
    releases, _ = github
    assert releases.latest() == {"version": "1.0.0", "name": None, "body": None, "html_url": None, "published_at": None,
                                 "fetched_at": None}

  def test_a_fetched_release_is_kept_without_its_comments_and_a_new_version_is_pushed(self, github, signals):
    releases, answers = github
    answers.append(answer())
    assert releases.refresh() == releases.latest() == {
        "version": "1.2.3", "name": "Sage 1.2.3", "body": "## Changes\n\nFaster.", "html_url": RELEASE["html_url"],
        "published_at": RELEASE["published_at"], "fetched_at": "2026-01-01T12:00:00"}
    assert (releases.latest_version, signals) == (
        "1.2.3", [(UpdateSource.RELEASE, None), (UpdateSource.LATEST_VERSION, None)])

  def test_the_same_version_again_or_the_running_one_is_a_release_and_no_new_version(self, github, signals):
    releases, answers = github
    answers.extend([answer(tag_name="v1.0.0"), answer(), answer()])
    for _ in range(3):
      releases.refresh()
    assert (releases.latest_version, signals) == ("1.2.3", [
        (UpdateSource.RELEASE, None), (UpdateSource.RELEASE, None), (UpdateSource.LATEST_VERSION, None),
        (UpdateSource.RELEASE, None)])

  def test_a_release_without_a_tag_keeps_the_version_known(self, github, signals):
    releases, answers = github
    answers.extend([answer(), answer(tag_name=None, body=None)])
    releases.refresh()
    releases.refresh()
    assert (releases.latest_version, releases.latest()["version"], releases.latest()["body"]) == ("1.2.3", "", None)

  @pytest.mark.parametrize("failure", [httpx.Response(403, json={"message": "API rate limit exceeded"}), httpx.ConnectError("offline")],
                           ids=["refused", "unreachable"])
  def test_a_failed_fetch_keeps_the_last_release_and_pushes_nothing(self, github, signals, failure, caplog):
    releases, answers = github
    answers.extend([answer(), failure])
    kept = releases.refresh()
    signals.clear()
    assert (releases.refresh(), releases.latest_version, signals) == (kept, "1.2.3", [])
    assert "Failed to fetch the latest Sage release" in caplog.text
