from datetime import datetime, timedelta

from services.db.models import Notification

T0 = datetime(2026, 1, 1, 12, 0)


def notices(client, **params):
  return [row["content"] for row in client.get("/api/notifications/", params=params).json()]


def test_the_notices_come_newest_first_and_as_many_as_asked(client, db):
  for minute in range(3):
    Notification.create(content=f"n{minute}", type="info", created_at=T0 + timedelta(minutes=minute))
  assert notices(client) == ["n2", "n1", "n0"]
  assert notices(client, limit=2) == ["n2", "n1"]


def test_a_window_holds_the_notices_after_its_start_and_up_to_its_end(client, db):
  for minute in range(4):
    Notification.create(content=f"n{minute}", type="info", created_at=T0 + timedelta(minutes=minute))
  assert notices(client, from_ts=(T0 + timedelta(minutes=1)).isoformat(),
                 to_ts=(T0 + timedelta(minutes=2)).isoformat()) == ["n2"]
