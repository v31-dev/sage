import re
from datetime import datetime

import pytest
from peewee import IntegrityError

from services.db.models import Application, Backup, Container, ContainerEvent, Domain, Notification, Project, Task, Volume, Worker, WorkerEvent
from utils.common import utcnow

PAST = datetime(2000, 1, 2, 3, 4, 5)
OPAQUE_ID = re.compile(r"[0-9a-f]{16}")


@pytest.fixture
def container(application, worker):
  return Container.create(application=application, worker=worker)


class TestBaseModel:
  def test_both_timestamps_are_set_when_a_row_is_created(self, db):
    before = utcnow()
    project = Project.create(name="demo", label="Demo")
    after = utcnow()

    assert before <= project.created_at <= after
    assert before <= project.updated_at <= after

  def test_saving_moves_updated_at_forward_and_leaves_created_at(self, project):
    Project.update(created_at=PAST, updated_at=PAST).execute()
    stored = Project.get_by_id("demo")
    before = utcnow()

    stored.label = "Renamed"
    stored.save()

    saved = Project.get_by_id("demo")
    assert saved.created_at == PAST
    assert saved.updated_at >= before

  def test_only_the_changed_fields_are_written(self, project):
    first, second = Project.get_by_id("demo"), Project.get_by_id("demo")

    first.label = "From the first"
    first.save()
    second.description = "From the second"
    second.save()

    saved = Project.get_by_id("demo")
    assert (saved.label, saved.description) == ("From the first", "From the second")


class TestProject:
  def test_a_project_needs_a_label(self, db):
    with pytest.raises(IntegrityError):
      Project.create(name="demo")

  def test_the_name_is_unique(self, project):
    with pytest.raises(IntegrityError):
      Project.create(name="demo", label="Another")

  @pytest.mark.parametrize("name", ["Demo", "my-project", "1abc", "has space", ""])
  def test_a_name_must_be_lowercase_letters_and_digits_starting_with_a_letter(self, db, name):
    with pytest.raises(ValueError, match="Invalid name"):
      Project.create(name=name, label="x")

  def test_env_is_read_back_unchanged(self, project):
    project.env = "A=1\nB=two words\n# note"
    project.save()
    assert Project.get_by_id("demo").env == "A=1\nB=two words\n# note"

  def test_an_invalid_env_is_refused_and_not_stored(self, project):
    project.env = "A=1\n9BAD=2"

    with pytest.raises(ValueError, match="project.env"):
      project.save()

    assert Project.get_by_id("demo").env is None

  def test_env_is_encrypted_in_the_database(self, db, project):
    project.env = "PASSWORD=a-very-distinctive-secret"
    project.save()

    raw = db.execute_sql("SELECT env FROM project WHERE name = 'demo'").fetchone()[0]

    assert "a-very-distinctive-secret" not in raw


class TestApplication:
  def test_the_name_is_unique_within_a_project(self, project, application):
    with pytest.raises(IntegrityError):
      Application.create(project=project, name="web", label="Again")

  def test_the_same_name_can_be_used_in_another_project(self, application):
    other = Project.create(name="other", label="Other")
    Application.create(project=other, name="web", label="Web")

  def test_a_project_with_applications_cannot_be_deleted(self, project, application):
    with pytest.raises(IntegrityError):
      project.delete_instance()

  @pytest.mark.parametrize("field", ["env", "args", "build_secrets"])
  def test_env_style_fields_are_checked_and_the_error_names_the_field(self, project, field):
    with pytest.raises(ValueError, match=f"application.{field}"):
      Application.create(project=project, name="git", label="Git", type="git", **{field: "9BAD=1"})

  @pytest.mark.parametrize("field", ["env", "args", "build_secrets"])
  def test_env_style_fields_are_read_back_unchanged_and_encrypted(self, db, application, field):
    setattr(application, field, "TOKEN='a-very-distinctive-secret' # kept")
    application.save()

    assert getattr(Application.get_by_id(application.id), field) == "TOKEN='a-very-distinctive-secret' # kept"
    raw = db.execute_sql(f"SELECT {field} FROM application WHERE id = ?", (application.id,)).fetchone()[0]
    assert "a-very-distinctive-secret" not in raw


class TestContainer:
  def test_its_id_is_opaque_and_a_recreated_placement_gets_a_new_one(self, application, worker):
    first = Container.create(application=application, worker=worker)
    first.delete_instance()
    second = Container.create(application=application, worker=worker)

    assert OPAQUE_ID.fullmatch(first.id) and OPAQUE_ID.fullmatch(second.id)
    assert first.id != second.id

  def test_one_container_per_application_and_worker(self, container, application, worker):
    with pytest.raises(IntegrityError):
      Container.create(application=application, worker=worker)

  def test_an_application_can_run_on_two_workers(self, container, application):
    Container.create(application=application, worker=Worker.create(hostname="w2", subdomain="blue"))

  def test_an_application_with_a_container_cannot_be_deleted(self, container, application):
    with pytest.raises(IntegrityError):
      application.delete_instance()

  def test_a_worker_with_a_container_cannot_be_deleted(self, container, worker):
    with pytest.raises(IntegrityError):
      worker.delete_instance()


class TestDomain:
  @pytest.fixture
  def other_worker(self, db):
    return Worker.create(hostname="w2", subdomain="blue")

  def test_its_id_is_opaque(self, container):
    assert OPAQUE_ID.fullmatch(Domain.create(name="shop", type="public", port=80, container=container).id)

  def test_deleting_its_container_unlinks_it_and_an_internal_one_keeps_its_namespace(self, container, worker):
    public = Domain.create(name="shop", type="public", port=80, container=container)
    internal = Domain.create(name="shop", type="internal", port=80, container=container, namespace_worker=worker)

    container.delete_instance()

    public, internal = Domain.get_by_id(public.id), Domain.get_by_id(internal.id)
    assert (public.container_id, internal.container_id) == (None, None)
    assert internal.namespace_worker_id == worker.hostname

  def test_a_worker_whose_namespace_holds_a_domain_cannot_be_deleted(self, worker):
    Domain.create(name="shop", type="internal", port=80, namespace_worker=worker)

    with pytest.raises(IntegrityError):
      worker.delete_instance()

  def test_a_public_name_is_taken_once(self, db):
    Domain.create(name="shop", type="public", port=80)

    with pytest.raises(IntegrityError):
      Domain.create(name="shop", type="public", port=81)

  @pytest.mark.parametrize("first, second", [("internal", "internal"), ("internal", "tcp"), ("tcp", "internal")])
  def test_an_internal_or_tcp_name_is_taken_once_in_a_workers_namespace(self, worker, first, second):
    Domain.create(name="shop", type=first, port=80, namespace_worker=worker)

    with pytest.raises(IntegrityError):
      Domain.create(name="shop", type=second, port=81, namespace_worker=worker)

  def test_the_same_name_can_be_in_two_workers_namespaces_and_public(self, worker, other_worker):
    Domain.create(name="shop", type="internal", port=80, namespace_worker=worker)
    Domain.create(name="shop", type="tcp", port=80, namespace_worker=other_worker)
    Domain.create(name="shop", type="public", port=80)

    assert Domain.select().where(Domain.name == "shop").count() == 3

  @pytest.mark.parametrize("port", [1, 65535])
  def test_a_port_in_range_is_accepted(self, db, port):
    Domain.create(name="shop", type="public", port=port)

  @pytest.mark.parametrize("port", [0, 65536, -1])
  def test_a_port_out_of_range_is_refused(self, db, port):
    with pytest.raises(IntegrityError, match="CHECK constraint failed"):
      Domain.create(name="shop", type="public", port=port)

  @pytest.mark.parametrize("name", ["shop", "shop-eu", "1shop", "a" * 63, "a-b-c"])
  def test_a_name_service_and_pool_are_any_valid_domain_label(self, db, name):
    Domain.create(name=name, type="public", port=80, service=name, pool=name)

  @pytest.mark.parametrize("name", ["Shop", "-shop", "shop-", "a" * 64, "shop_eu", "shop.eu", ""])
  @pytest.mark.parametrize("field", ["name", "service", "pool"])
  def test_anything_else_is_refused(self, db, field, name):
    with pytest.raises(ValueError, match="lowercase letters, digits and dashes, at most 63"):
      Domain.create(**{"name": "shop", "type": "public", "port": 80, field: name})


class TestWorker:
  def test_a_worker_needs_a_subdomain(self, db):
    with pytest.raises(IntegrityError, match="NOT NULL constraint failed: worker.subdomain"):
      Worker.create(hostname="w1")

  @pytest.mark.parametrize("value", ["EU1", "-eu1", "eu1-", "a" * 64, "eu_1"])
  def test_a_subdomain_is_a_valid_domain_label(self, db, value):
    with pytest.raises(ValueError):
      Worker.create(hostname="w1", subdomain=value)

  def test_two_workers_cannot_share_a_subdomain(self, db):
    Worker.create(hostname="w1", subdomain="eu1")

    with pytest.raises(IntegrityError):
      Worker.create(hostname="w2", subdomain="eu1")


class TestVolume:
  def test_its_id_is_opaque(self, application):
    assert OPAQUE_ID.fullmatch(Volume.create(application=application, name="data", path="/data").id)

  def test_the_name_is_unique_within_an_application(self, application):
    Volume.create(application=application, name="data", path="/data")

    with pytest.raises(IntegrityError):
      Volume.create(application=application, name="data", path="/other")

  def test_volumes_go_with_their_application(self, application):
    Volume.create(application=application, name="data", path="/data")

    application.delete_instance()

    assert Volume.select().count() == 0


class TestBackup:
  def test_backups_go_with_their_application(self, application):
    Backup.create(type="application", s3_path="backups/applications/x.tar.gz.enc", application=application)

    application.delete_instance()

    assert Backup.select().count() == 0

  def test_a_backup_keeps_its_source_volume_after_the_volume_is_deleted(self, application):
    volume = Volume.create(application=application, name="data", path="/data")
    backup = Backup.create(type="application", s3_path="backups/x", application=application,
                           source_volume_id=volume.id, source_volume_name="data")

    volume.delete_instance()

    stored = Backup.get_by_id(backup.id)
    assert (stored.source_volume_id, stored.source_volume_name) == (volume.id, "data")


@pytest.fixture(params=["container", "worker"])
def subject(request, application, worker):
  """An event model, its subject and the fields of one event about that subject."""
  if request.param == "container":
    container = Container.create(application=application, worker=worker)
    return ContainerEvent, container, dict(container=container, subject_id=container.id, action="deploy",
                                           application_name="demo-web", worker_hostname=worker.hostname)
  return WorkerEvent, worker, dict(worker=worker, subject_id=worker.hostname, action="reboot")


class TestEvents:
  def test_an_event_outlives_its_subject_and_keeps_what_it_was(self, subject):
    model, resource, fields = subject
    event = model.create(transition="succeeded", operation_id="op1", **fields)

    resource.delete_instance()

    stored = model.get_by_id(event.id)
    link = "container_id" if model is ContainerEvent else "worker_id"
    assert getattr(stored, link) is None
    assert (stored.subject_id, stored.action, stored.transition, stored.operation_id) == (
        fields["subject_id"], fields["action"], "succeeded", "op1")
    if model is ContainerEvent:
      assert (stored.application_name, stored.worker_hostname) == ("demo-web", "w1")

  def test_the_transitions_of_one_operation_share_its_id(self, subject):
    model, _, fields = subject
    for transition in ("admitted", "failed", "recovered"):
      model.create(transition=transition, operation_id="op1", **fields)

    assert [event.transition for event in model.select().where(model.operation_id == "op1").order_by(model.id)] == [
        "admitted", "failed", "recovered"]

  def test_a_dedupe_key_is_recorded_once_and_events_without_one_repeat(self, subject):
    model, _, fields = subject
    model.create(transition="succeeded", dedupe_key="op1:succeeded", **fields)
    model.create(transition="observed", **fields)
    model.create(transition="observed", **fields)

    with pytest.raises(IntegrityError):
      model.create(transition="succeeded", dedupe_key="op1:succeeded", **fields)


@pytest.fixture
def one_row_each(container, worker):
  Domain.create(name="shop", type="public", port=80)
  Backup.create(s3_path="backups/x")
  Notification.create(content="hello")
  Task.create(task_id="t1", name="deploy", executor="common", status="completed")
  ContainerEvent.create(container=container, subject_id=container.id, action="deploy", transition="succeeded",
                        application_name="demo-web", worker_hostname=worker.hostname)
  WorkerEvent.create(worker=worker, subject_id=worker.hostname, action="reboot", transition="succeeded")


@pytest.mark.parametrize("table, column", [
    ("application", "type"), ("domain", "type"), ("backup", "type"), ("notification", "type"), ("task", "status"),
    ("container_event", "action"), ("container_event", "transition"), ("worker_event", "action"),
    ("worker_event", "transition"),
])
def test_the_database_refuses_a_value_outside_a_columns_choices(db, one_row_each, table, column):
  with pytest.raises(IntegrityError, match="CHECK constraint failed"):
    db.execute_sql(f'UPDATE "{table}" SET "{column}" = ?', ("bogus",))


@pytest.mark.parametrize("table, column", [("project", "application_count"), ("application", "container_count")])
def test_the_database_refuses_a_negative_count(db, application, table, column):
  with pytest.raises(IntegrityError, match="CHECK constraint failed"):
    db.execute_sql(f'UPDATE "{table}" SET "{column}" = -1')


class TestTask:
  def test_the_json_fields_are_read_back_as_they_were_written(self, db):
    Task.create(task_id="t1", name="deploy", scopes=["application:1"], params={"force": True, "names": ["a"]}, executor="common",
                status="completed")

    task = Task.get_by_id("t1")

    assert task.scopes == ["application:1"]
    assert task.params == {"force": True, "names": ["a"]}

  def test_the_task_id_is_unique(self, db):
    Task.create(task_id="t1", name="deploy", executor="common", status="completed")

    with pytest.raises(IntegrityError):
      Task.create(task_id="t1", name="deploy", executor="common", status="completed")
