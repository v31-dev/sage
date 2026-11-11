"""Projects and applications as their pages show them: an application around the payloads of its containers."""
from services.db.models import Application, Project, Volume


def project_payload(project: Project) -> dict:
  """Its configuration and how many applications it has."""
  return {
      "name": project.name,
      "label": project.label,
      "description": project.description,
      "env": project.env,
      "application_count": project.application_count,
      "created_at": project.created_at,
      "updated_at": project.updated_at,
  }


def project_list() -> list[dict]:
  """Every project's payload, by name."""
  return [project_payload(project) for project in Project.select().order_by(Project.name)]


def application_summary(application: Application, active_count: int) -> dict:
  """The application as a list of applications shows it: what it runs, and how many of its containers are active: an
  application has no state of its own."""
  return {
      "id": application.id,
      "name": application.name,
      "label": application.label,
      "description": application.description,
      "type": application.type,
      "image": application.image,
      "repo": application.repo,
      "container_count": application.container_count,
      "active_count": active_count,
      "created_at": application.created_at,
      "updated_at": application.updated_at,
  }


def application_payload(application: Application, volumes: list[Volume], containers: list[dict]) -> dict:
  """Its summary with the rest of its configuration, its project and volumes, and the payloads of its containers."""
  return {
      **application_summary(application, sum(container["view"]["headline"] == "active" for container in containers)),
      "path": application.path,
      "env": application.env,
      "args": application.args,
      "build_secrets": application.build_secrets,
      "command": application.command,
      "project": {"name": application.project.name, "label": application.project.label},
      "volumes": [{"id": volume.id, "name": volume.name, "path": volume.path, "backup_cron": volume.backup_cron,
                   "created_at": volume.created_at, "updated_at": volume.updated_at} for volume in volumes],
      "containers": containers,
  }
