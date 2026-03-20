import os
import logging
import httpx
from pathlib import Path
from datetime import datetime, timedelta
from peewee import CharField, FloatField, IntegerField, DateTimeField, TextField, SqliteDatabase, Model, CompositeKey
from playhouse.sqlite_ext import FTS5Model, SearchField, RowIDField

from utils.common import get_env
from services.base import Base


app_dir = Path(__file__).parent.parent
logger = logging.getLogger(__name__)

# delta is period start end range
# buckets is number of points to aggregate
# points is number of data points to return
# data in db is stored in minute intervals
_PERIODS = {
  '1m':  {'delta': timedelta(minutes=1), 'bucket': 1, 'points': 1},
  '1h':  {'delta': timedelta(hours=1),  'bucket': 1, 'points': 60},
  '24h': {'delta': timedelta(hours=24), 'bucket': 24, 'points': 60},
  '1w':  {'delta': timedelta(days=7),  'bucket': 168, 'points': 60},         
}

class Metrics(Base):
  def __init__(self):
    super().__init__()

    self.config_path = '/etc/vector'
    os.makedirs(self.config_path, exist_ok=True)
    with open(app_dir / "templates/manager/vector/vector.yml", "r") as f:
      vector_config = f.read()
      vector_config = vector_config.replace("${HOSTNAME}", get_env("HOSTNAME"))
      vector_config = vector_config.replace("${IP}", 'sage')
      with open(f"{self.config_path}/vector.yaml", "w") as f:
        f.write(vector_config)

    self.db_path = '/app/data/metrics'
    os.makedirs(f"{self.db_path}/metrics", exist_ok=True)
    os.makedirs(f"{self.db_path}/logs", exist_ok=True)

    self._dbs = {
      'metrics': {},
      'logs': {}
    }

  def get_metrics_db(self, hostname):
    '''
      1 database per hostname
    '''
    with self.lock:
      if hostname not in self._dbs['metrics']:
        path = f"{self.db_path}/metrics/{hostname}.db"
        db = SqliteDatabase(path, pragmas={
          'journal_mode': 'wal',
          'cache_size': -2000,
          'foreign_keys': 1,
          'busy_timeout': 5000,
        })

        class BaseModel(Model):
          class Meta:
            database = db

        class WorkerMeta(BaseModel):
          id            = IntegerField(primary_key=True)  # always row id=1
          cpu_cores     = IntegerField()
          mem_total_mb  = IntegerField()
          disk_total_gb = FloatField()

        class WorkerMetrics(BaseModel):
          ts            = DateTimeField(primary_key=True)
          cpu_pct       = FloatField()
          mem_used_mb   = IntegerField()
          mem_cached_mb = IntegerField()
          disk_used_gb  = FloatField()
          load_avg_1m   = FloatField()
          load_avg_5m   = FloatField()
          load_avg_15m  = FloatField()
          net_rx_kbps   = FloatField()
          net_tx_kbps   = FloatField()

        class ContainerMetrics(BaseModel):
          name          = CharField()
          ts            = DateTimeField()
          cpu_pct       = FloatField()
          mem_used_mb   = FloatField()
          net_rx_kbps   = FloatField()
          net_tx_kbps   = FloatField()
          
          class Meta:
            primary_key = CompositeKey('name', 'ts')

        db.connect(reuse_if_open=True)
        db.create_tables([WorkerMeta, WorkerMetrics, ContainerMetrics], safe=True)
        self._dbs['metrics'][hostname] = {
          'db': db,
          'models': {
            'WorkerMeta': WorkerMeta,
            'WorkerMetrics': WorkerMetrics,
            'ContainerMetrics': ContainerMetrics
          }
        }
    
    return self._dbs['metrics'][hostname]
  
  def get_logs_db(self, container):
    '''
      1 database per container.
      ContainerLogs stores all fields; ContainerLogsIndex is an FTS5 virtual table
      that indexes only the message text for fast full-text search.
    '''
    with self.lock:
      if container not in self._dbs['logs']:
        path = f"{self.db_path}/logs/{container}.db"
        db = SqliteDatabase(path, pragmas={
          'journal_mode': 'wal',
          'cache_size': -2048,   # 2MB — FTS index thrashing prevented; room for B-tree
          'busy_timeout': 5000,  # Wait up to 5s before returning SQLITE_BUSY.
          'synchronous': 1,      # NORMAL — commit writes faster, acceptable for logs
        })

        class BaseModel(Model):
          class Meta:
            database = db

        class ContainerLogs(BaseModel):
          hostname  = CharField()
          ts        = CharField()  # preserves Vector nanosecond precision
          stream    = CharField()
          message   = TextField()

        class ContainerLogsIndex(FTS5Model):
          rowid   = RowIDField()
          message = SearchField()

          class Meta:
            database = db
            options  = {'content': ContainerLogs, 'content_rowid': 'id'}

        db.connect(reuse_if_open=True)
        db.create_tables([ContainerLogs, ContainerLogsIndex], safe=True)
        # Trigger keeps FTS index in sync incrementally — O(new rows) per insert,
        # not O(total rows) like rebuild() would be.
        db.execute_sql("""
          CREATE TRIGGER IF NOT EXISTS containerlogs_ai
          AFTER INSERT ON containerlogs BEGIN
            INSERT INTO containerlogsindex(rowid, message) VALUES (new.id, new.message);
          END
        """)
        # Keep the index clean when logs are deleted
        db.execute_sql("""
            CREATE TRIGGER IF NOT EXISTS containerlogs_ad
            AFTER DELETE ON containerlogs BEGIN
              INSERT INTO containerlogsindex(containerlogsindex, rowid, message) 
              VALUES('delete', old.id, old.message);
            END;
        """)
        self._dbs['logs'][container] = {
          'db': db,
          'models': {
            'ContainerLogs':      ContainerLogs,
            'ContainerLogsIndex': ContainerLogsIndex,
          }
        }
    
    return self._dbs['logs'][container]

  def collect(self, ip, hostname):
    metrics_endpoint = f"http://{ip}:61208/api/4"
    plugins = ["cpu", "mem", "fs", "load", "network", "containers"]
    ts = datetime.now().replace(second=0, microsecond=0)

    try:
      with httpx.Client(timeout=10) as client:
        data = {ep: client.get(f"{metrics_endpoint}/{ep}").json() for ep in plugins}

      root_fs = max(data['fs'], key=lambda f: f.get("size", 0))
      db_info = self.get_metrics_db(hostname)
      WorkerMeta = db_info['models']['WorkerMeta']
      WorkerMetrics = db_info['models']['WorkerMetrics']
      ContainerMetrics = db_info['models']['ContainerMetrics']

      WorkerMeta.replace(
        id=1,
        cpu_cores=data['cpu']['cpucore'],
        mem_total_mb=data['mem']['total'] // (1024**2),
        disk_total_gb=round(root_fs["size"] / (1024**3), 2)
      ).execute()

      WorkerMetrics.replace(
        ts=ts,
        cpu_pct=data['cpu']['total'],
        mem_used_mb=data['mem']['used'] // (1024**2),
        mem_cached_mb=data['mem']['cached'] // (1024**2),
        disk_used_gb=round(root_fs["used"] / (1024**3), 2),
        load_avg_1m=data['load']['min1'],
        load_avg_5m=data['load']['min5'],
        load_avg_15m=data['load']['min15'],
        net_rx_kbps=round(sum(i.get("bytes_recv_rate_per_sec", 0) for i in data['network'] if i.get("interface_name") != "lo") / 1024, 2),
        net_tx_kbps=round(sum(i.get("bytes_sent_rate_per_sec", 0) for i in data['network'] if i.get("interface_name") != "lo") / 1024, 2)
      ).execute()

      cpu_cores = data['cpu']['cpucore']
      for ct in data['containers']:
        cpu_total = ct.get('cpu', {}).get('total', 0)
        mem_usage = ct.get('memory', {}).get('usage', 0)
        net_rx = 0
        net_tx = 0

        if cpu_cores > 0:
          cpu_total = round(cpu_total / cpu_cores, 2)

        if ct.get('network'):
          net_rx = round(ct['network'].get('rx', 0) / 1024, 2)
          net_tx = round(ct['network'].get('tx', 0) / 1024, 2)

        ContainerMetrics.replace(
          ts=ts,
          name=ct['name'],
          cpu_pct=cpu_total,
          mem_used_mb=mem_usage // (1024**2),
          net_rx_kbps=net_rx,
          net_tx_kbps=net_tx
        ).execute()

      logger.info(f"Collected metrics for {hostname} ({len(data['containers'])} containers) at {ts.isoformat()}.")
    except Exception as e:
      raise Exception(f"Failed to collect metrics for {hostname}: {e}")
    
  def cleanup(self):
    cutoff = datetime.now() - timedelta(days=7)

    # Cleanup metrics from all shards
    deleted_w = 0
    deleted_c = 0
    for hostname, db_info in self._dbs['metrics'].items():
      WorkerMetrics = db_info['models']['WorkerMetrics']
      ContainerMetrics = db_info['models']['ContainerMetrics']
      deleted_w += WorkerMetrics.delete().where(WorkerMetrics.ts < cutoff).execute()
      deleted_c += ContainerMetrics.delete().where(ContainerMetrics.ts < cutoff).execute()
    logger.info(f"Metrics cleanup: removed {deleted_w} worker rows, {deleted_c} container rows older than 7 days.")

    # Cleanup logs from all open shards
    deleted_l = 0
    for container, db_info in self._dbs['logs'].items():
      ContainerLogs = db_info['models']['ContainerLogs']
      deleted_l += ContainerLogs.delete().where(ContainerLogs.ts < cutoff.isoformat()).execute()
    logger.info(f"Logs cleanup: removed {deleted_l} log rows older than 7 days.")

  def query_period(self, hostname: str, period: str = '1h'):
    period_config = _PERIODS.get(period, _PERIODS['1h'])

    # Snap now to previous minute for consistent bucketing
    now = datetime.now().replace(second=0, microsecond=0)
    since = now - period_config['delta']

    # Get the db shard for this hostname
    db_info = self.get_metrics_db(hostname)
    WorkerMeta = db_info['models']['WorkerMeta']
    WorkerMetrics = db_info['models']['WorkerMetrics']
    ContainerMetrics = db_info['models']['ContainerMetrics']

    rows = list(
      WorkerMetrics
        .select()
        .where(WorkerMetrics.ts >= since)
        .order_by(WorkerMetrics.ts)
        .dicts()
    )

    # Format timestamps once — compute template with None values for missing fields
    metric_fields = {k for k in WorkerMetrics._meta.fields.keys() if k != 'ts'}
    empty_row_template = {}
    for k in metric_fields:
      empty_row_template[k] = None
    
    for r in rows:
      r['ts'] = r['ts'].isoformat() + 'Z'
    
    # Fill in missing buckets with ts (creates one-minute rows)
    rows = self._fill_missing_values(rows, now, period_config['bucket'] * period_config['points'], empty_row_template)

    # Aggregate metrics into period buckets
    rows = self._aggregate_buckets(rows, period_config['bucket'], ['ts'])

    # Container series — group by name, same bucketing
    containers = list(ContainerMetrics.select(ContainerMetrics.name).where(ContainerMetrics.ts >= since).distinct().dicts())
    containers = [self.query_container_period(hostname, c['name'], period) for c in containers]

    # Get static metadata for capacity context
    meta = WorkerMeta.select().dicts().first() or {}

    return {
      'host': rows,
      'containers': containers,
      'meta': meta
    }
  
  def _aggregate_buckets(self, rows, bucket, agg_fields):
    '''
      Aggregate rows into buckets by averaging values within each bucket time range.
      Aggregate all fields except for `agg_fields` fields. 
      Aggregate functions will be average, if any bucket has missing values for a field, the aggregated value will be null.
    '''
    keys = set(rows[0].keys()) - set(agg_fields) if rows else set()

    # Create the aggregate list
    agg_rows_length = len(rows) // bucket
    agg_rows = []

    for i in range(agg_rows_length):
      bucket_rows = rows[i*bucket:(i+1)*bucket]
      agg_row = {k: bucket_rows[0][k] for k in agg_fields}

      for k in keys:
        values = [r[k] for r in bucket_rows if r[k] is not None]
        agg_row[k] = round(sum(values) / len(values), 2) if values else None
      agg_rows.append(agg_row)

    return agg_rows

  def _fill_missing_values(self, rows, start, target_points, template):
    '''
      Fill rows with missing time periods (ts) in increments of 1 min.
      Creates target_points rows spanning the full period.
    '''
    result = []
    current = start
    idx = len(rows) - 1  # Start from the newest row (end of list)
    while len(result) < target_points:
      current_iso = current.isoformat() + 'Z'
      if idx >= 0 and rows[idx]['ts'] == current_iso:
        result.append(rows[idx])
        idx -= 1  # Move backwards through rows
      else:
        # Insert empty row for missing bucket
        result.append({'ts': current_iso, **template})
      current -= timedelta(minutes=1)

    return list(reversed(result))

  def query_container_period(self, hostname: str, container: str, period: str = '1h'):
    period_config = _PERIODS.get(period, _PERIODS['1h'])
    
    # Snap now to previous minute for consistent bucketing
    now = datetime.now().replace(second=0, microsecond=0)
    since = now - period_config['delta']

    # Get the db shard for this hostname
    db_info = self.get_metrics_db(hostname)
    ContainerMetrics = db_info['models']['ContainerMetrics']

    rows = list(
      ContainerMetrics
        .select()
        .where((ContainerMetrics.name == container) & (ContainerMetrics.ts >= since))
        .order_by(ContainerMetrics.ts)
        .dicts()
    )

    # Format timestamps once — compute template with None values for missing fields
    metric_fields = {k for k in ContainerMetrics._meta.fields.keys() if k not in {'ts', 'name'}}
    empty_row_template = {'name': container}
    for k in metric_fields:
      empty_row_template[k] = None
    
    for r in rows:
      r['ts'] = r['ts'].isoformat() + 'Z'
    
    # Fill in missing buckets with ts (creates one-minute rows)
    rows = self._fill_missing_values(rows, now, period_config['bucket'] * period_config['points'], empty_row_template)

    # Aggregate metrics into period buckets
    rows = self._aggregate_buckets(rows, period_config['bucket'], ['ts', 'name'])

    return rows

  def write_logs(self, container: str, entries: list):
    shard = self.get_logs_db(container)
    ContainerLogs = shard['models']['ContainerLogs']
    with shard['db'].atomic():
      ContainerLogs.insert_many(entries).execute()
      # FTS index updated automatically via containerlogs_ai trigger

    logger.info(f"Wrote {len(entries)} log entries for container {container}.")

  def query_logs(self, container: str, hostname: str = '', search: str = '',  from_ts: str = '', to_ts: str = '') -> list:
    '''
      If `hostname` is provided, filters to only that host's entries.
      If `search` is provided, filters via FTS5 full-text index on message.
      If `from_ts` and `to_ts` are provided, returns logs within that range.
    '''
    shard = self.get_logs_db(container)
    ContainerLogs      = shard['models']['ContainerLogs']
    ContainerLogsIndex = shard['models']['ContainerLogsIndex']

    # Build base query with FTS if searching
    if search:
      query = (ContainerLogs
        .select()
        .join(ContainerLogsIndex, on=(ContainerLogs.id == ContainerLogsIndex.rowid))
        .where(ContainerLogsIndex.match(search)))
    else:
      query = ContainerLogs.select()

    # Apply common filters
    if hostname:
      query = query.where(ContainerLogs.hostname == hostname)
    if from_ts:
      query = query.where(ContainerLogs.ts >= from_ts)
    if to_ts:
      query = query.where(ContainerLogs.ts < to_ts)

    rows = query.order_by(ContainerLogs.ts.asc()).dicts()

    return list(rows)
