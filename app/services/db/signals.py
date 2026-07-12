from peewee import fn
from playhouse.signals import post_delete, post_save

from .models import Application, Container, Project


def _application_count(project_id):
  return Application.select(fn.COUNT(Application.id)).where(Application.project == project_id)


def _container_count(application_id):
  return Container.select(fn.COUNT(Container.id)).where(Container.application == application_id)


@post_save(sender=Application)
def post_save_application(model_class, instance, created):
  if created:
    Project.update(application_count=_application_count(instance.project_id)).where(
        Project.name == instance.project_id
    ).execute()


@post_delete(sender=Application)
def post_delete_application(model_class, instance):
  Project.update(application_count=_application_count(instance.project_id)).where(
      Project.name == instance.project_id
  ).execute()


@post_save(sender=Container)
def post_save_container(model_class, instance, created):
  if created:
    Application.update(container_count=_container_count(instance.application_id)).where(
        Application.id == instance.application_id
    ).execute()


@post_delete(sender=Container)
def post_delete_container(model_class, instance):
  Application.update(container_count=_container_count(instance.application_id)).where(
      Application.id == instance.application_id
  ).execute()

  # Last container gone -> nothing to run, so the app is inactive.
  Application.update(status="inactive").where(
      (Application.id == instance.application_id) & (Application.container_count == 0)
  ).execute()
