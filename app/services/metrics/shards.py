"""A host's metrics file: its size, its minutes and its containers' minutes."""
from peewee import CharField, CompositeKey, DateTimeField, FloatField, IntegerField, Model, SqliteDatabase


def open_shard(path: str) -> dict:
  """The host's file at `path`, created when missing: its database and its models by name."""
  db = SqliteDatabase(
      path,
      pragmas={
          "journal_mode": "wal",
          "cache_size": -2000,
          "foreign_keys": 1,
          "busy_timeout": 5000,
      },
  )

  class BaseModel(Model):
    class Meta:
      database = db

  class WorkerMeta(BaseModel):
    id = IntegerField(primary_key=True)  # always row id=1
    cpu_cores = IntegerField()
    mem_total_mb = IntegerField()
    disk_total_gb = FloatField()

  class WorkerMetrics(BaseModel):
    ts = DateTimeField(primary_key=True)
    cpu_pct = FloatField()
    mem_used_mb = IntegerField()
    mem_cached_mb = IntegerField()
    disk_used_gb = FloatField()
    load_avg_1m = FloatField(null=True)
    load_avg_5m = FloatField(null=True)
    load_avg_15m = FloatField(null=True)
    net_rx_kbps = FloatField()
    net_tx_kbps = FloatField()

  class ContainerMetrics(BaseModel):
    name = CharField()
    ts = DateTimeField()
    cpu_pct = FloatField()
    mem_used_mb = FloatField()
    net_rx_kbps = FloatField()
    net_tx_kbps = FloatField()

    class Meta:
      primary_key = CompositeKey("name", "ts")

  db.connect(reuse_if_open=True)
  db.create_tables([WorkerMeta, WorkerMetrics, ContainerMetrics], safe=True)
  return {
      "db": db,
      "models": {
          "WorkerMeta": WorkerMeta,
          "WorkerMetrics": WorkerMetrics,
          "ContainerMetrics": ContainerMetrics,
      },
  }
