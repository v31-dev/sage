import pytest

from services.applications.rules import apply_type, validate_backup_cron, validate_image, validate_repo, validate_volume_path


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
