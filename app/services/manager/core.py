import logging
from datetime import datetime, timedelta

import httpx
from peewee import fn

from services.db import Application, Backup, Container, Domain, Event, Notification, Project, Worker
from services.notification import Notifications
from utils.executor import NOTIFICATIONS_EXECUTOR, submit_with_context

logger = logging.getLogger(__name__)

LATEST_RELEASE_URL = "https://api.github.com/repos/v31-dev/sage/releases/latest"


class CoreMixin:
  def get_latest_version(self):
    try:
      with httpx.Client(timeout=10) as client:
        response = client.get(
            LATEST_RELEASE_URL,
            headers={"Accept": "application/vnd.github+json"},
        )
        response.raise_for_status()
        tag = (response.json().get("tag_name") or "").lstrip("v")
      if tag:
        self.latest_version = tag
    except Exception as e:
      logger.warning(f"Failed to fetch latest sage release: {e}")
    return self.latest_version

  async def async_init(self):
    """
    Perform async initialization after Manager.__init__().
    - Discovers and reconciles S3 backups with the database.
    """
    try:
      await self.discover_s3_platform_backups()
    except Exception as e:
      logger.error(f"S3 backup discovery failed during startup: {e}")

  def notify(self, message: str, type: str = "info", link: str | None = None):
    # Python logging has no success level; treat it as info.
    log_method = logger.info if type == "success" else getattr(logger, type, logger.info)
    log_method(message)
    notification = Notification.create(content=message, type=type, link=link)
    submit_with_context(NOTIFICATIONS_EXECUTOR, Notifications().dispatch, notification)

  def _summary_notification_dicts(self, query, limit: int):
    return list(query.order_by(Notification.created_at.desc()).limit(limit).dicts())

  def _status_counts(self, model):
    rows = (
        model.select(model.status, fn.COUNT(model.id).alias("count"))
        .group_by(model.status)
        .dicts()
    )
    return {row["status"]: row["count"] for row in rows}

  def get_system_summary(self, lookback_hours: int = 24):
    now = datetime.now()
    since = now - timedelta(hours=lookback_hours)

    worker_total = Worker.select().count()
    worker_online = Worker.select().where(Worker.online).count()
    application_total = Application.select().count()
    application_counts = self._status_counts(Application)
    application_active = application_counts.get("active", 0)
    application_inactive = application_counts.get("inactive", 0)
    application_error = application_counts.get("error", 0)
    application_deploying = application_counts.get("deploying", 0)
    application_stopping = application_counts.get("stopping", 0)
    application_backup = application_counts.get("backup", 0)
    application_restoring = application_counts.get("restoring", 0)
    container_total = Container.select().count()
    container_counts = self._status_counts(Container)
    container_active = container_counts.get("active", 0)
    container_inactive = container_counts.get("inactive", 0)
    container_error = container_counts.get("error", 0)
    container_deploying = container_counts.get("deploying", 0)
    container_stopping = container_counts.get("stopping", 0)
    container_backup = container_counts.get("backup", 0)
    container_restoring = container_counts.get("restoring", 0)
    domain_active = Domain.select().join(Application).where(Application.domains_synced).count()
    domain_inactive = Domain.select().join(Application).where(Application.domains_synced == False).count()
    backup_system = Backup.select().where(Backup.type == "platform").count()
    backup_application = Backup.select().where(Backup.type == "application").count()
    deployments_last_24h = Event.select().where(
        (Event.type == "deploy") & (Event.created_at >= since)
    ).count()
    critical_events = list(Notification.select().where(
        (Notification.created_at >= since)
        & (Notification.type.in_(["error", "warning"]))
    ).order_by(Notification.created_at.desc()).dicts())
    critical_error_count = sum(1 for e in critical_events if e["type"] == "error")
    critical_warning_count = sum(1 for e in critical_events if e["type"] == "warning")
    latest_backup = Backup.select().order_by(Backup.created_at.desc()).first()

    if worker_total == 0:
      worker_offline = 0
    else:
      worker_offline = max(worker_total - worker_online, 0)

    return {
        "generated_at": now,
        "workers_total": worker_total,
        "workers_online": worker_online,
        "workers_offline": worker_offline,
        "projects_total": Project.select().count(),
        "applications_total": application_total,
        "applications_active": application_active,
        "applications_inactive": application_inactive,
        "applications_error": application_error,
        "applications_deploying": application_deploying,
        "applications_stopping": application_stopping,
        "applications_backup": application_backup,
        "applications_restoring": application_restoring,
        "deployments_last_24h": deployments_last_24h,
        "containers_total": container_total,
        "containers_active": container_active,
        "containers_inactive": container_inactive,
        "containers_error": container_error,
        "containers_deploying": container_deploying,
        "containers_stopping": container_stopping,
        "containers_backup": container_backup,
        "containers_restoring": container_restoring,
        "domains_total": domain_active + domain_inactive,
        "domains_active": domain_active,
        "domains_inactive": domain_inactive,
        "backups_total": backup_system + backup_application,
        "backups_system": backup_system,
        "backups_application": backup_application,
        "backups_last_24h": Backup.select().where(Backup.created_at >= since).count(),
        "latest_backup_at": latest_backup.created_at if latest_backup else None,
        "critical_error_count_last_24h": critical_error_count,
        "critical_warning_count_last_24h": critical_warning_count,
        "critical_events_last_24h": critical_events,
    }

  def send_summary_notification(self):
    summary = self.get_system_summary(lookback_hours=24)

    error_count = summary["critical_error_count_last_24h"]
    warning_count = summary["critical_warning_count_last_24h"]
    critical_events = summary["critical_events_last_24h"]

    lines = [
        f"Daily system summary ({summary['generated_at'].strftime('%Y-%m-%d %H:%M UTC')})",
        "",
        f"Workers: {summary['workers_online']}/{summary['workers_total']} online, {summary['workers_offline']} offline",
        f"Projects: {summary['projects_total']} total",
        (
            "Apps: "
            f"{summary['applications_total']} total, "
            f"{summary['applications_active']} active, "
            f"{summary['applications_inactive']} inactive, "
            f"{summary['applications_error']} error, "
            f"{summary['applications_deploying']} deploying, "
            f"{summary['applications_stopping']} stopping, "
            f"{summary['applications_backup']} backup, "
            f"{summary['applications_restoring']} restoring"
        ),
        (
            "Containers: "
            f"{summary['containers_total']} total, "
            f"{summary['containers_active']} active, "
            f"{summary['containers_inactive']} inactive, "
            f"{summary['containers_error']} error, "
            f"{summary['containers_deploying']} deploying, "
            f"{summary['containers_stopping']} stopping, "
            f"{summary['containers_backup']} backup, "
            f"{summary['containers_restoring']} restoring"
        ),
        (
            "Domains: "
            f"{summary['domains_total']} total, "
            f"{summary['domains_active']} synced, "
            f"{summary['domains_inactive']} unsynced"
        ),
        (
            "Backups: "
            f"{summary['backups_total']} total, "
            f"{summary['backups_system']} system, "
            f"{summary['backups_application']} application"
        ),
        f"Events in last 24h: {error_count} errors, {warning_count} warnings",
    ]

    notification_type = "info"
    if error_count > 0:
      notification_type = "error"
    elif warning_count > 0:
      notification_type = "warning"

    submit_with_context(
        NOTIFICATIONS_EXECUTOR, Notifications().dispatch,
        {"content": "\n".join(lines), "type": notification_type})
