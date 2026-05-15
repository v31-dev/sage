from playhouse.signals import post_delete, post_save, pre_save

from .models import Application, Container, Domain, Notification, Project


@pre_save(sender=Application)
def pre_save_application(model_class, instance, created):
  if not created:
    if "domains_synced" not in [f.name for f in instance.dirty_fields]:
      instance.domains_synced = False


@post_save(sender=Application)
def post_save_application(model_class, instance, created):
  if created:
    Project.update(application_count=Project.application_count + 1).where(
        Project.name == instance.project_id
    ).execute()


@post_delete(sender=Application)
def post_delete_application(model_class, instance):
  Project.update(application_count=Project.application_count - 1).where(
      Project.name == instance.project_id
  ).execute()


@post_save(sender=Domain)
def set_domains_synced_false_on_update_on_domain(model_class, instance, created):
  Application.update(domains_synced=False).where(
      Application.id == instance.application_id
  ).execute()


@post_save(sender=Container)
def post_save_container(model_class, instance, created):
  if created:
    Application.update(container_count=Application.container_count + 1).where(
        Application.id == instance.application_id
    ).execute()

  if instance.application.container_count == 0:
    Application.update(status="inactive").where(
        Application.id == instance.application_id
    ).execute()

  Application.update(domains_synced=False).where(
      Application.id == instance.application_id
  ).execute()


@post_delete(sender=Container)
def post_delete_container(model_class, instance):
  Application.update(container_count=Application.container_count - 1).where(
      Application.id == instance.application_id
  ).execute()
