import hashlib
import json
from dataclasses import dataclass

from services.db.models import Application, Project, Volume, db
from values.names import qualified_name


@dataclass(frozen=True)
class DeployConfig:
  """An application's deploy inputs at one instant: a deploy reads this one view while edits stay instant and apply
  to the next deploy. `volumes` are (volume id, mount path) pairs, sorted. `content_hash` covers what changes the
  deployment; `qualified_name` only locates it."""
  qualified_name: str
  type: str
  image: str | None
  repo: str | None
  path: str
  env: str
  args: str
  build_secrets: str
  command: str
  project_env: str
  volumes: tuple[tuple[str, str], ...]

  @property
  def content_hash(self) -> str:
    payload = json.dumps(
        {
            "type": self.type,
            "image": self.image,
            "repo": self.repo,
            "path": self.path,
            "env": self.env,
            "args": self.args,
            "build_secrets": self.build_secrets,
            "command": self.command,
            "project_env": self.project_env,
            "volumes": [list(volume) for volume in self.volumes],
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()

  def check_deployable(self):
    """ValueError unless the inputs can be deployed: a Docker application needs an image, a Git one its repository
    and Dockerfile path."""
    if self.type == "docker" and not (self.image or "").strip():
      raise ValueError("Docker applications must have an image specified.")
    if self.type == "git" and not ((self.repo or "").strip() and self.path.strip()):
      raise ValueError("Git applications must have a repo & path specified.")


def capture_deploy_config(application_id: int) -> DeployConfig:
  """The application's deploy inputs, read in one transaction; LookupError when it is gone."""
  with db.atomic():
    application = Application.select(Application, Project).join(Project).where(Application.id == application_id).first()
    if application is None:
      raise LookupError("Application not found.")
    volumes = tuple(sorted(Volume.select(Volume.id, Volume.path).where(Volume.application == application_id).tuples()))
  return DeployConfig(
      qualified_name=qualified_name(application.project.name, application.name),
      type=application.type,
      image=application.image,
      repo=application.repo,
      path=application.path,
      env=application.env or "",
      args=application.args or "",
      build_secrets=application.build_secrets or "",
      command=application.command or "",
      project_env=application.project.env or "",
      volumes=volumes,
  )
