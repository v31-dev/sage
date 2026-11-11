"""The summary of the platform: what it holds, how it is now and what happened lately, for the home page and the daily
message to the notification channels."""
from collections import Counter
from datetime import timedelta

from services.containers.decisions import HEADLINES
from services.db.models import Application, Backup, Container, ContainerEvent, Domain, Notification, Project
from services.domains.decisions import STATUSES as DOMAIN_STATUSES
from services.notification import Notifications
from services.settings import Settings
from services.workers.decisions import STATUSES as WORKER_STATUSES
from utils.common import utcnow
from utils.executor import NOTIFICATIONS_EXECUTOR, submit_with_context
from utils.queue import OnConflict

# A container's headlines while an operation on it is under way, which the daily message counts together.
IN_PROGRESS = ("deploying", "stopping", "backup", "restoring")


def counts(values, vocabulary: tuple) -> dict:
  """How many of `values` there are, and how many of each of `vocabulary`, every one of which is listed."""
  counted = Counter(values)
  return {"total": sum(counted.values()), **{value: counted[value] for value in vocabulary}}


class SummaryMixin:
  def get_system_summary(self, lookback_hours: int = 24) -> dict:
    """What the platform holds; its workers, containers and Domains by their states now; and in the last
    `lookback_hours`, the deploys that ended (an interrupted one failed), the backups and the error and warning
    notices."""
    now = utcnow()
    since = now - timedelta(hours=lookback_hours)
    deploys = Counter("succeeded" if transition == "succeeded" else "failed" for (transition,) in ContainerEvent.select(
        ContainerEvent.transition).where((ContainerEvent.action == "deploy") & (ContainerEvent.created_at >= since)
                                         & ContainerEvent.transition.in_(("succeeded", "failed", "interrupted"))).tuples())
    backups = Counter(Backup.select(Backup.type).scalars())
    latest_backup = Backup.select().order_by(Backup.created_at.desc()).first()
    notices = list(Notification.select().where((Notification.created_at >= since)
                                               & Notification.type.in_(("error", "warning")))
                   .order_by(Notification.created_at.desc()).dicts())
    return {
        "generated_at": now,
        "projects": Project.select().count(),
        "applications": Application.select().count(),
        "workers": counts((state.view.status for state in self.worker_states().values()), WORKER_STATUSES),
        "containers": counts((state.view.headline for state in self.container_states(Container.select()).values()),
                             HEADLINES),
        "domains": counts((state.view.status for state in self.domain_states(Domain.select()).values()), DOMAIN_STATUSES),
        "deploys": {"succeeded": deploys["succeeded"], "failed": deploys["failed"]},
        "backups": {"platform": backups["platform"], "application": backups["application"],
                    "since": Backup.select().where(Backup.created_at >= since).count(), "latest_at": latest_backup and latest_backup.created_at},
        "notices": {"error": sum(notice["type"] == "error" for notice in notices),
                    "warning": sum(notice["type"] == "warning" for notice in notices), "events": notices},
    }

  def request_summary(self) -> str | None:
    """Queues the daily summary to the notification channels; None when none is set. The task's id."""
    if not Settings().is_configured("notifications"):
      return None
    return self.add_task(task=self.send_summary_notification, key=("send_summary_notification",), scopes=set(),
                         executor="common", on_conflict=OnConflict.REPLACE).task_id

  def send_summary_notification(self):
    """Sends the summary of the last 24 hours to the notification channels, with no notice of its own. It is an error
    when a notice was, or a container or Domain is in error or a worker offline; a warning when a notice was, or a
    worker is otherwise not online."""
    summary = self.get_system_summary(lookback_hours=24)
    workers, containers, domains = summary["workers"], summary["containers"], summary["domains"]
    backups, deploys, notices = summary["backups"], summary["deploys"], summary["notices"]

    def parts(entries) -> str:
      shown = [f"{count} {name}" for name, count in entries if count]
      return f" ({', '.join(shown)})" if shown else ""

    in_progress = sum(containers[headline] for headline in IN_PROGRESS)
    lines = [
        f"Daily summary ({summary['generated_at'].strftime('%Y-%m-%d %H:%M UTC')})",
        "",
        f"Workers: {workers['online']} of {workers['total']} online"
        + parts((status, workers[status]) for status in WORKER_STATUSES if status != "online"),
        f"Projects: {summary['projects']}, applications: {summary['applications']}",
        f"Containers: {containers['total']}" + parts([
            *((headline, containers[headline]) for headline in ("active", "inactive", "error")),
            ("in progress", in_progress), ("checking", containers["checking"])]),
        f"Domains: {domains['total']}" + parts((status, domains[status]) for status in DOMAIN_STATUSES),
        f"Deploys in the last 24h: {deploys['succeeded']} succeeded, {deploys['failed']} failed",
        f"Backups: {backups['platform']} platform, {backups['application']} volume archives; {backups['since']} in the "
        "last 24h",
        f"Notices in the last 24h: {notices['error']} errors, {notices['warning']} warnings",
    ]
    if notices["error"] or containers["error"] or domains["error"] or workers["offline"]:
      severity = "error"
    elif notices["warning"] or workers["online"] < workers["total"]:
      severity = "warning"
    else:
      severity = "info"
    submit_with_context(NOTIFICATIONS_EXECUTOR, Notifications().dispatch, {"content": "\n".join(lines), "type": severity})
