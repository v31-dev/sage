import pytest

from services.applications.rules import (MAX_RESPONSE_HEADERS, apply_type, response_headers, validate_backup_cron, validate_image, validate_repo,
                                         validate_response_headers, validate_volume_path)
from values.headers import APPLICATION_OWN_HEADERS, SECURITY_HEADERS


@pytest.mark.parametrize("image", ["nginx", "nginx:1.27", "ghcr.io/org/app:v1.2", "registry.test:5000/app@sha256:abc123", "a_b-c.d", None, ""])
def test_an_image_reference_is_accepted(image):
  assert validate_image(image) == image


@pytest.mark.parametrize("image", ["-nginx", "nginx latest", "nginx;rm -rf /", "$(whoami)", "nginx\nlatest", "image`id`", "nginx'"])
def test_an_image_reference_with_odd_characters_is_refused(image):
  with pytest.raises(ValueError, match="Invalid image reference"):
    validate_image(image)


@pytest.mark.parametrize("repo", [
    "https://github.com/user/repo.git",
    "http://git.example.test/user/repo.git",
    "https://github.com/user/repo.git#main",
    "https://github.com/user/repo.git#release-1.2",
    "https://github.com/user/repo.git:sub/dir",
    "https://github.com/user/repo.git#main:sub/dir",
    None,
])
def test_a_repository_url_in_the_documented_format_is_accepted(repo):
  assert validate_repo(repo) == repo


@pytest.mark.parametrize("repo", [
    "git@github.com:user/repo.git",
    "https://github.com/user/repo",
    "https://github.com/repo.git",
    "https://github.com/group/sub/repo.git",
    "ftp://github.com/user/repo.git",
    "https://github.com/user/repo.git#",
    "https://github.com/user/repo.git extra",
    "https://github.com/user/repo.git\n--evil",
    "https://github.com/user/repo.git\n",
    "https://github.com/user/repo.git#main\n",
])
def test_any_other_repository_url_is_refused(repo):
  with pytest.raises(ValueError, match="Invalid Git repository URL"):
    validate_repo(repo)


def test_a_docker_application_builds_nothing():
  config = {"type": "docker", "image": "nginx", "repo": "https://github.com/user/repo.git", "path": "other/Dockerfile",
            "args": "A=1", "build_secrets": "S=1"}
  apply_type(config)
  assert config == {"type": "docker", "image": "nginx", "repo": None, "path": "Dockerfile", "args": None,
                    "build_secrets": None}


def test_a_git_application_pulls_no_image_and_keeps_its_build_inputs():
  config = {"type": "git", "image": "nginx", "repo": "https://github.com/user/repo.git", "path": "docker/Dockerfile",
            "args": "A=1", "build_secrets": "S=1"}
  apply_type(config)
  assert config == {"type": "git", "image": None, "repo": "https://github.com/user/repo.git", "path": "docker/Dockerfile",
                    "args": "A=1", "build_secrets": "S=1"}


@pytest.mark.parametrize("path", ["/data", "/var/lib/my app", "/", "/a/b/c.d", "/data/"])
def test_an_absolute_path_is_accepted(path):
  assert validate_volume_path(path) == path


@pytest.mark.parametrize("path", ["data", "./data", "", None, "/data:ro", "/data:/other", '/da"ta', "/da\nta", "/da\rta"])
def test_a_relative_path_or_one_with_a_colon_quote_or_line_break_is_refused(path):
  with pytest.raises(ValueError, match="Invalid volume path"):
    validate_volume_path(path)


@pytest.mark.parametrize("expression", [None, "", "   "])
def test_a_blank_backup_cron_means_no_scheduled_backup(expression):
  assert validate_backup_cron(expression) is None


@pytest.mark.parametrize("expression", ["0 3 * * *", "*/15 * * * 1-5"])
def test_a_five_part_cron_is_kept(expression):
  assert validate_backup_cron(expression) == expression


@pytest.mark.parametrize("expression, message", [
    ("0 3 * *", "5-part"),
    ("0 0 3 * * *", "5-part"),
    (5, "5-part"),
    ("61 * * * *", "Invalid backup_cron"),
])
def test_any_other_backup_cron_is_refused(expression, message):
  with pytest.raises(ValueError, match=message):
    validate_backup_cron(expression)


class TestResponseHeaders:
  def test_a_default_is_changed_spelled_as_sage_spells_it_and_trimmed(self):
    assert validate_response_headers({"content-security-policy": " default-src 'self' https://cdn.test "}) == {
        "Content-Security-Policy": "default-src 'self' https://cdn.test"}

  @pytest.mark.parametrize("name", ["X-Request-Source", "x-app-version"])
  def test_an_x_header_is_the_applications_own(self, name):
    assert validate_response_headers({name: "v1"}) == {name: "v1"}

  @pytest.mark.parametrize("headers", [
      None, {}, {"Set-Cookie": "a=b"}, {"Access-Control-Allow-Origin": "*"}, {"": "v"}, {"Bad Name": "v"},
      {"Permissions-Policy": None}, {"X-A": None}, {"X-A": "   "}, {"X-A": 3}, {"X-A": {"nested": "v"}},
      {"x-frame-options": " DENY "}, {"defaults": dict(SECURITY_HEADERS), "changes": {"X-A": "v"}},
  ], ids=["none", "empty", "another-header", "a-cors-header", "empty-name", "another-name-with-a-space", "a-null-default",
          "a-null-x", "blank", "a-number", "an-object", "the-default", "the-payload-echoed"])
  def test_what_holds_no_change_is_dropped(self, headers):
    assert validate_response_headers(headers) is None

  @pytest.mark.parametrize("name", ["Content-Security-Policy", "content-security-policy"])
  def test_a_null_csp_is_sent_by_the_application_itself(self, name):
    assert validate_response_headers({name: None, "Permissions-Policy": None}) == {"Content-Security-Policy": None}

  def test_a_dropped_header_leaves_the_others(self):
    assert validate_response_headers({"Set-Cookie": "a=b", "Referrer-Policy": None, "X-App": "v1",
                                      "X-Frame-Options": "SAMEORIGIN"}) == {"X-App": "v1", "X-Frame-Options": "SAMEORIGIN"}

  @pytest.mark.parametrize("headers, message", [
      (["X-A"], "must be an object"),
      ({"X-Bad Name": "v"}, "Invalid header name"),
      ({"X-A": "one\r\nSet-Cookie: a=b"}, "one line"),
      ({"X-A": "a\x00b"}, "one line"),
      ({"Content-Security-Policy": "v" * 4097}, "one line"),
      ({"x-frame-options": "DENY", "X-Frame-Options": "SAMEORIGIN"}, "given twice"),
      ({"X-App": None, "x-app": "b"}, "given twice"),
      ({f"X-H{n}": "v" for n in range(MAX_RESPONSE_HEADERS + 1)}, "at most"),
  ], ids=["not-a-map", "an-x-name-with-a-space", "a-line-break", "a-nul", "too-long", "a-default-twice", "an-x-twice",
          "too-many"])
  def test_a_header_sage_cannot_send_is_refused(self, headers, message):
    with pytest.raises(ValueError, match=message):
      validate_response_headers(headers)

  def test_as_many_headers_as_allowed_are_kept_past_dropped_ones(self):
    headers = {"Set-Cookie": "a=b", **{f"X-H{n}": "v" for n in range(MAX_RESPONSE_HEADERS)}}
    assert len(validate_response_headers(headers)) == MAX_RESPONSE_HEADERS

  def test_the_defaults_go_with_the_changes_over_them(self):
    sent = response_headers({"Content-Security-Policy": "default-src *", "X-App": "v1"})
    assert sent == {**SECURITY_HEADERS, "Content-Security-Policy": "default-src *", "X-App": "v1"}

  def test_a_header_the_application_sends_itself_is_left_out(self):
    sent = response_headers({"Content-Security-Policy": None, "X-App": "v1"})
    assert "Content-Security-Policy" not in sent and sent == {**{name: value for name, value in SECURITY_HEADERS.items()
                                                                 if name not in APPLICATION_OWN_HEADERS}, "X-App": "v1"}

  def test_without_changes_every_default_is_sent(self):
    assert response_headers(None) == SECURITY_HEADERS
