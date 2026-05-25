from playhouse.signals import post_delete, post_save, pre_save

from .models import Application, Container, Domain, Notification, Project


def _trigger_application_domain_sync(application_id):
  Application.update(domains_synced=False).where(
      Application.id == application_id).execute()


@pre_save(sender=Application)
def pre_save_application(model_class, instance, created):
  if not created:
    dirty_field_names = {field.name for field in instance.dirty_fields}
    if "status" in dirty_field_names and "domains_synced" not in dirty_field_names:
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
def post_save_domain(model_class, instance, created):
  _trigger_application_domain_sync(instance.application_id)


@post_delete(sender=Domain)
def post_delete_domain(model_class, instance):
  _trigger_application_domain_sync(instance.application_id)


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

  _trigger_application_domain_sync(instance.application_id)


@post_delete(sender=Container)
def post_delete_container(model_class, instance):
  Application.update(container_count=Application.container_count - 1).where(
      Application.id == instance.application_id
  ).execute()
