from services.applications.rules import apply_type, validate_backup_cron, validate_image, validate_repo, validate_volume_path
from services.applications.updates import application_changed, container_changed, hosts_changed
from services.db.models import Application, Container, Project, Volume, Worker
from services.db.writes import get, merged, save_changes, trim, write
from utils.errors import Conflict
from values.names import derive_name

PROJECT_FIELDS = ("label", "description", "env")
APPLICATION_FIELDS = ("label", "description", "type", "image", "repo", "path", "env", "args", "build_secrets", "command")
TYPE_FIELDS = {"type", "image", "repo", "path", "args", "build_secrets"}
VOLUME_FIELDS = ("name", "path", "backup_cron")


def _count_applications(project_name: str):
  Project.update(application_count=Application.select().where(Application.project == project_name).count()).where(
      Project.name == project_name).execute()


def _count_containers(application_id: int):
  Application.update(container_count=Container.select().where(Container.application == application_id).count()).where(
      Application.id == application_id).execute()


def create_project(label: str, description: str | None = None) -> Project:
  label, description = trim(label), trim(description)
  name = derive_name(label)
  with write():
    if Project.select().where(Project.name == name).exists():
      raise Conflict(f"A project named '{name}' already exists.")
    return Project.create(name=name, label=label, description=description)


def patch_project(name: str, fields: dict) -> Project:
  with write():
    project = get(Project, Project.name == name)
    changed = save_changes(project, merged(project, fields, PROJECT_FIELDS))
    applications = list(project.applications) if changed else []
  for application in applications:
    hosts_changed(application)
  return project


def delete_project(name: str):
  with write():
    project = get(Project, Project.name == name)
    if project.applications.exists():
      raise Conflict("A project with applications can't be deleted.")
    project.delete_instance()


def create_application(project_name: str, label: str, description: str | None = None) -> Application:
  label, description = trim(label), trim(description)
  name = derive_name(label)
  with write():
    project = get(Project, Project.name == project_name)
    if project.applications.where(Application.name == name).exists():
      raise Conflict(f"An application named '{name}' already exists in this project.")
    application = Application.create(project=project, name=name, label=label, description=description)
    _count_applications(project_name)
  application_changed(application)
  return application


def patch_application(application_id: int, fields: dict) -> Application:
  with write():
    application = get(Application, Application.id == application_id)
    config = merged(application, fields, APPLICATION_FIELDS)
    if TYPE_FIELDS & set(fields):
      apply_type(config)
      validate_image(config["image"])
      validate_repo(config["repo"])
    changed = save_changes(application, config)
  if changed:
    hosts_changed(application)
  return application


def delete_application(application_id: int):
  with write():
    application = get(Application, Application.id == application_id)
    if application.containers.exists():
      raise Conflict("An application with containers can't be deleted.")
    application.delete_instance()
    _count_applications(application.project_id)
  application_changed(application)


def create_volume(application_id: int, name: str, path: str, backup_cron: str | None = None) -> Volume:
  name, path, backup_cron = trim(name), trim(path), trim(backup_cron)
  if name is None:
    raise ValueError("name is required.")
  with write():
    application = get(Application, Application.id == application_id)
    if application.volumes.where(Volume.name == name).exists():
      raise Conflict(f"A volume named '{name}' already exists in this application.")
    volume = Volume.create(application=application, name=name, path=validate_volume_path(path),
                           backup_cron=validate_backup_cron(backup_cron))
  application_changed(application)
  return volume


def patch_volume(volume_id: str, fields: dict) -> Volume:
  with write():
    volume = get(Volume, Volume.id == volume_id)
    config = merged(volume, fields, VOLUME_FIELDS)
    validate_volume_path(config["path"])
    config["backup_cron"] = validate_backup_cron(config["backup_cron"])
    if config["name"] != volume.name and Volume.select().where(
            (Volume.application == volume.application_id) & (Volume.name == config["name"])).exists():
      raise Conflict(f"A volume named '{config['name']}' already exists in this application.")
    changed = save_changes(volume, config)
  if changed:
    application_changed(volume.application)
  return volume


def delete_volume(volume_id: str):
  with write():
    volume = get(Volume, Volume.id == volume_id)
    volume.delete_instance()
  application_changed(volume.application)


def attach_container(application_id: int, worker_hostname: str) -> Container:
  with write():
    application = get(Application, Application.id == application_id)
    worker = Worker.get_or_none(Worker.hostname == worker_hostname)
    if worker is None:
      raise LookupError(f"Worker with hostname '{worker_hostname}' not found.")
    if application.containers.where(Container.worker == worker).exists():
      raise Conflict(f"Container already exists on worker '{worker_hostname}'.")
    container = Container.create(application=application, worker=worker)
    _count_containers(application_id)
  container_changed(container)
  return container


def detach_container(container_id: str) -> list[str]:
  """Deletes the container's row, which unlinks its domains, and returns their ids."""
  with write():
    container = get(Container, Container.id == container_id)
    domains = [domain.id for domain in container.domains]
    container.delete_instance()
    _count_containers(container.application_id)
  container_changed(container)
  return domains
