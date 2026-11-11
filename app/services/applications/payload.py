"""The application as its page shows it, around the payloads of its containers."""
from services.db.models import Application, Volume


def application_payload(application: Application, volumes: list[Volume], containers: list[dict]) -> dict:
  """Its configuration, its project and volumes, the payloads of its containers, and how many of them are active: an
  application has no state of its own."""
  return {
      "id": application.id,
      "name": application.name,
      "label": application.label,
      "description": application.description,
      "type": application.type,
      "image": application.image,
      "repo": application.repo,
      "path": application.path,
      "env": application.env,
      "args": application.args,
      "build_secrets": application.build_secrets,
      "command": application.command,
      "project": {"name": application.project.name, "label": application.project.label},
      "volumes": [{"id": volume.id, "name": volume.name, "path": volume.path, "backup_cron": volume.backup_cron,
                   "created_at": volume.created_at, "updated_at": volume.updated_at} for volume in volumes],
      "containers": containers,
      "container_count": application.container_count,
      "active_count": sum(container["view"]["headline"] == "active" for container in containers),
      "created_at": application.created_at,
      "updated_at": application.updated_at,
  }
