import asyncio
import logging
import posixpath
import shlex
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import asyncssh
import pytest

import config
from services import tailscale as tailscale_module
from services.tailscale import Tailscale, next_online, ping_path
from utils.executor import active_executor

HOST = "worker-1"


class FakeProcess:
  """What `create_process` returns: `lines` come out of `stdout` one by one, then it waits for
  `gate` (when given) before it ends, or raises `error`."""

  def __init__(self, lines=(), status=0, gate=None, error=None):
    self.lines = list(lines)
    self.status = status
    self.gate = gate
    self.error = error
    self.closed = False
    self.stdout = self._stream()

  async def _stream(self):
    for line in self.lines:
      yield line
    if self.gate is not None:
      await self.gate.wait()
    if self.error is not None:
      raise self.error

  async def wait(self):
    return SimpleNamespace(exit_status=self.status)

  def close(self):
    self.closed = True


class FakeConn:
  """A pooled connection: each `create_process` call gives the next of `outcomes`, a process or
  an exception to raise."""

  def __init__(self, *outcomes):
    self.outcomes = list(outcomes)
    self.calls = []

  def is_closed(self):
    return False

  async def create_process(self, command, **kwargs):
    self.calls.append((command, kwargs))
    outcome = self.outcomes.pop(0)
    if isinstance(outcome, Exception):
      raise outcome
    return outcome


@pytest.fixture
def tailscale(monkeypatch):
  service = Tailscale()
  service.evicted = []

  async def evict(hostname):
    service.evicted.append(hostname)

  monkeypatch.setattr(service, "_evict", evict)
  return service


def use(tailscale, *conns):
  """The pool hands out these connections in turn."""
  pending = list(conns)

  async def get_conn(hostname):
    return pending.pop(0)

  tailscale._get_conn = get_conn


async def run(tailscale, outcome, command="docker ps", **kwargs):
  conn = FakeConn(outcome)
  use(tailscale, conn)
  return conn, await tailscale.exec_command(HOST, command, **kwargs)


async def test_the_command_runs_under_timeout_through_the_sessions_shell(tailscale):
  command = """echo 'a "b"' && echo $(date) | tr a-z A-Z"""
  conn, _ = await run(tailscale, FakeProcess(), command, timeout=120)

  sent, kwargs = conn.calls[0]
  assert shlex.split(sent) == ["timeout", "-k", str(config.COMMAND_KILL_AFTER_SECONDS), "120", "${SHELL:-/bin/sh}", "-c", command]
  assert kwargs == {"stderr": asyncssh.STDOUT, "errors": "replace"}


async def test_the_default_deadline_is_five_minutes(tailscale):
  conn, _ = await run(tailscale, FakeProcess())

  assert shlex.split(conn.calls[0][0])[3] == "300"


@pytest.mark.parametrize("timeout", [0, -5])
async def test_a_deadline_that_is_not_positive_is_refused(tailscale, timeout):
  # `timeout 0` runs the command with no deadline
  use(tailscale, FakeConn())
  with pytest.raises(ValueError, match="positive"):
    await tailscale.exec_command(HOST, "docker ps", timeout=timeout)


async def test_it_returns_the_exit_status_and_the_output_lines(tailscale):
  _, result = await run(tailscale, FakeProcess(["first\n", "second\n", "last"]))

  assert result == (0, ["first", "second", "last"])


async def test_an_empty_line_in_the_output_is_kept(tailscale):
  _, (_, lines) = await run(tailscale, FakeProcess(["a\n", "\n", "b\n"]))

  assert lines == ["a", "", "b"]


async def test_carriage_returns_split_a_line_and_the_client_version_warning_is_dropped(tailscale):
  _, (_, lines) = await run(tailscale, FakeProcess(
      ["step 1\rstep 2\r\n", "Warning: client version 1.2 is older than the server\n", "done\n"]))

  assert lines == ["step 1", "step 2", "done"]


async def test_each_line_is_logged_when_it_arrives_not_when_the_command_ends(tailscale, caplog):
  gate = asyncio.Event()
  conn = FakeConn(FakeProcess(["building\n"], gate=gate))
  use(tailscale, conn)

  with caplog.at_level(logging.INFO, logger="services.tailscale"):
    task = asyncio.create_task(tailscale.exec_command(HOST, "docker compose up"))
    for _ in range(20):
      await asyncio.sleep(0)
    assert f"[{HOST}] building" in caplog.text
    assert not task.done()

    gate.set()
    assert await task == (0, ["building"])


async def test_a_failing_command_raises_with_the_tail_of_its_output(tailscale):
  lines = [f"line {number}\n" for number in range(1, 16)]

  with pytest.raises(Exception) as raised:
    await run(tailscale, FakeProcess(lines, status=3))

  message = str(raised.value)
  assert "exit code 3" in message
  assert "line 15" in message and "line 6" in message
  assert "line 5" not in message


async def test_a_failing_command_without_output_names_only_the_exit_code(tailscale):
  with pytest.raises(Exception) as raised:
    await run(tailscale, FakeProcess(status=1))

  assert str(raised.value) == f"Error executing command on {HOST}: exit code 1"


@pytest.mark.parametrize("status", [124, 137])
async def test_the_exit_status_timeout_gives_at_the_deadline_is_a_timeout(tailscale, monkeypatch, status):
  clock = iter([100.0, 100.0 + 60])
  monkeypatch.setattr(tailscale_module, "time", SimpleNamespace(monotonic=lambda: next(clock)))

  with pytest.raises(Exception, match=f"Command on {HOST} timed out after 60s: docker ps"):
    await run(tailscale, FakeProcess(status=status), timeout=60)


async def test_exit_124_long_before_the_deadline_is_the_commands_own_failure(tailscale):
  with pytest.raises(Exception, match="exit code 124"):
    await run(tailscale, FakeProcess(status=124), timeout=300)


async def test_a_worker_that_answers_nothing_is_given_up_on_after_the_grace(tailscale, monkeypatch):
  monkeypatch.setattr(config, "COMMAND_WAIT_GRACE_SECONDS", 0)
  process = FakeProcess(["working\n"], gate=asyncio.Event())
  conn = FakeConn(process)
  use(tailscale, conn)

  with pytest.raises(Exception, match=f"Command on {HOST} timed out after 0.05s: docker ps"):
    async with asyncio.timeout(2):  # the configured grace applies, not the default one
      await tailscale.exec_command(HOST, "docker ps", timeout=0.05)

  assert tailscale.evicted == [HOST]
  assert process.closed


async def test_the_manager_waits_past_the_deadline_for_the_workers_own_answer(tailscale, monkeypatch):
  monkeypatch.setattr(config, "COMMAND_WAIT_GRACE_SECONDS", 2)
  gate = asyncio.Event()
  asyncio.get_running_loop().call_later(0.2, gate.set)
  conn = FakeConn(FakeProcess(["done\n"], gate=gate))
  use(tailscale, conn)

  assert await tailscale.exec_command(HOST, "docker ps", timeout=0.05) == (0, ["done"])


async def test_a_connection_lost_while_the_command_runs_is_not_retried(tailscale):
  process = FakeProcess(["started\n"], error=asyncssh.ConnectionLost("gone"))
  conn = FakeConn(process, FakeProcess())
  use(tailscale, conn, conn)

  with pytest.raises(Exception, match=f"Error executing command on {HOST}: gone"):
    await tailscale.exec_command(HOST, "docker compose up")

  assert len(conn.calls) == 1
  assert tailscale.evicted == [HOST]
  assert process.closed


async def test_a_stale_connection_is_replaced_and_the_open_retried_once(tailscale):
  stale = FakeConn(asyncssh.ChannelOpenError(2, "connect failed"))
  fresh = FakeConn(FakeProcess(["ok\n"]))
  use(tailscale, stale, fresh)

  assert await tailscale.exec_command(HOST, "docker ps") == (0, ["ok"])

  assert len(stale.calls) == 1 and len(fresh.calls) == 1
  assert tailscale.evicted == [HOST]


async def test_an_open_that_fails_twice_raises(tailscale):
  first = FakeConn(OSError("reset"))
  second = FakeConn(asyncssh.ConnectionLost("gone"))
  use(tailscale, first, second)

  with pytest.raises(Exception, match=f"Error executing command on {HOST}: gone"):
    await tailscale.exec_command(HOST, "docker ps")

  assert tailscale.evicted == [HOST, HOST]


async def test_a_worker_that_cannot_be_reached_raises(tailscale):
  async def get_conn(hostname):
    raise OSError("no route")

  tailscale._get_conn = get_conn

  with pytest.raises(Exception, match=f"Error connecting to {HOST}: no route"):
    await tailscale.exec_command(HOST, "docker ps")


async def test_the_channel_is_closed_after_success_and_after_failure(tailscale):
  done = FakeProcess(["ok\n"])
  failed = FakeProcess(status=2)
  use(tailscale, FakeConn(done), FakeConn(failed))

  await tailscale.exec_command(HOST, "true")
  with pytest.raises(Exception):
    await tailscale.exec_command(HOST, "false")

  assert done.closed and failed.closed


async def test_the_channel_is_closed_when_the_task_is_cancelled(tailscale):
  process = FakeProcess(["working\n"], gate=asyncio.Event())
  use(tailscale, FakeConn(process))

  task = asyncio.create_task(tailscale.exec_command(HOST, "docker compose up"))
  for _ in range(20):
    await asyncio.sleep(0)
  task.cancel()

  with pytest.raises(asyncio.CancelledError):
    await task
  assert process.closed


class FakeSftpFile:
  def __init__(self, sftp, path, attrs):
    self.sftp = sftp
    self.path = path
    self.attrs = attrs

  async def __aenter__(self):
    # A new file takes the mode it is created with, a rewritten one keeps its mode (checked on a worker)
    mode = self.sftp.files[self.path]["mode"] if self.path in self.sftp.files else (self.attrs.permissions or 0o644)
    self.sftp.files[self.path] = {"data": b"", "mode": mode}
    return self

  async def __aexit__(self, *exc):
    return False

  async def write(self, data):
    if self.sftp.write_error is not None:
      raise self.sftp.write_error
    self.sftp.files[self.path]["data"] = data if isinstance(data, bytes) else data.encode()


class FakeSftp:
  """The worker's SFTP file system as checked on a worker: a rename replaces an existing file."""

  def __init__(self):
    self.files = {}
    self.directories = []
    self.renames = []
    self.write_error = None
    self.rename_error = None

  async def __aenter__(self):
    return self

  async def __aexit__(self, *exc):
    return False

  async def makedirs(self, path, attrs=None, exist_ok=False):
    self.directories.append(path)

  def open(self, path, flags="r", attrs=None):
    return FakeSftpFile(self, path, attrs or asyncssh.SFTPAttrs())

  async def put(self, source, destination):
    self.files[destination] = {"data": Path(source).read_bytes(), "mode": self.files.get(destination, {}).get("mode", 0o644)}

  async def posix_rename(self, old, new):
    if self.rename_error is not None:
      raise self.rename_error
    self.renames.append((old, new))
    self.files[new] = self.files.pop(old)

  async def remove(self, path):
    self.files.pop(path, None)


class FakeSftpConnection:
  def __init__(self):
    self.sftp = FakeSftp()

  def start_sftp_client(self):
    return self.sftp


@pytest.fixture
def sftp(tailscale):
  connection = FakeSftpConnection()
  use(tailscale, connection, connection)  # a failed upload is tried once more on a fresh connection
  return connection.sftp


@pytest.fixture
def template(tmp_path):
  path = tmp_path / "template"
  path.write_text("TOKEN=${CONTENT}")
  return path


DESTINATION = "/opt/sage/applications/demo-web/.env"


class TestSyncFileWithAMode:
  async def test_the_file_is_created_with_the_mode_and_renamed_into_place(self, tailscale, sftp, template):
    await tailscale.sync_file(HOST, template, DESTINATION, {"CONTENT": "abc"}, mode=0o600)

    assert sftp.files == {DESTINATION: {"data": b"TOKEN=abc", "mode": 0o600}}
    [(temporary, target)] = sftp.renames
    assert target == DESTINATION
    assert posixpath.dirname(temporary) == posixpath.dirname(DESTINATION)
    assert temporary != DESTINATION

  async def test_a_file_written_earlier_with_the_default_mode_is_replaced_not_rewritten(self, tailscale, sftp, template):
    sftp.files[DESTINATION] = {"data": b"old", "mode": 0o644}

    await tailscale.sync_file(HOST, template, DESTINATION, {"CONTENT": "new"}, mode=0o600)

    assert sftp.files[DESTINATION] == {"data": b"TOKEN=new", "mode": 0o600}

  async def test_a_script_gets_its_own_mode(self, tailscale, sftp, template):
    await tailscale.sync_file(HOST, template, "/tmp/sage-backup-1234.sh", {"CONTENT": "x"}, mode=0o700)

    assert sftp.files["/tmp/sage-backup-1234.sh"]["mode"] == 0o700

  async def test_a_file_copied_as_it_is_keeps_every_byte(self, tailscale, sftp, tmp_path):
    key = tmp_path / "key.pem"
    key.write_bytes(b"-----BEGIN PRIVATE KEY-----\n\xff\xfe\x00binary\n-----END PRIVATE KEY-----\n")

    await tailscale.sync_file(HOST, key, "/opt/sage/traefik/certs/key.pem", mode=0o600)

    assert sftp.files == {"/opt/sage/traefik/certs/key.pem": {"data": key.read_bytes(), "mode": 0o600}}

  async def test_the_parent_directory_is_made(self, tailscale, sftp, template):
    await tailscale.sync_file(HOST, template, DESTINATION, {"CONTENT": "x"}, mode=0o600)

    assert sftp.directories == ["/opt/sage/applications/demo-web"]

  async def test_a_failed_upload_leaves_no_temporary_file_and_the_old_one_alone(self, tailscale, sftp, template):
    sftp.files[DESTINATION] = {"data": b"old", "mode": 0o600}
    sftp.write_error = asyncssh.SFTPFailure("disk full")

    with pytest.raises(Exception, match="disk full"):
      await tailscale.sync_file(HOST, template, DESTINATION, {"CONTENT": "new"}, mode=0o600)

    assert sftp.files == {DESTINATION: {"data": b"old", "mode": 0o600}}

  async def test_a_failed_rename_leaves_no_temporary_file_either(self, tailscale, sftp, template):
    sftp.files[DESTINATION] = {"data": b"old", "mode": 0o600}
    sftp.rename_error = asyncssh.SFTPFailure("permission denied")

    with pytest.raises(Exception, match="permission denied"):
      await tailscale.sync_file(HOST, template, DESTINATION, {"CONTENT": "new"}, mode=0o600)

    assert sftp.files == {DESTINATION: {"data": b"old", "mode": 0o600}}


class TestSyncFileWithoutAMode:
  async def test_the_file_is_written_in_place_as_before(self, tailscale, sftp, template):
    sftp.files[DESTINATION] = {"data": b"old", "mode": 0o644}

    await tailscale.sync_file(HOST, template, DESTINATION, {"CONTENT": "new"})

    assert sftp.files == {DESTINATION: {"data": b"TOKEN=new", "mode": 0o644}}
    assert sftp.renames == []

  async def test_a_file_without_substitutions_is_copied_whole(self, tailscale, sftp, template):
    await tailscale.sync_file(HOST, template, DESTINATION)

    assert sftp.files == {DESTINATION: {"data": b"TOKEN=${CONTENT}", "mode": 0o644}}
    assert sftp.renames == []


class ClosableConnection:
  def __init__(self, close_error=None, wait_error=None):
    self.close_error = close_error
    self.wait_error = wait_error
    self.closed = False
    self.waited = False

  def close(self):
    if self.close_error is not None:
      raise self.close_error
    self.closed = True

  async def wait_closed(self):
    self.waited = True
    if self.wait_error is not None:
      raise self.wait_error


class TestClose:
  async def test_every_pooled_connection_is_closed_and_waited_for_and_the_pool_is_emptied(self, tailscale):
    first, second = ClosableConnection(), ClosableConnection()
    tailscale._conns = {"w1": first, "w2": second}

    await tailscale.close()

    assert (first.closed, first.waited, second.closed, second.waited) == (True, True, True, True)
    assert tailscale._conns == {}

  async def test_one_that_fails_to_close_does_not_stop_the_others(self, tailscale):
    broken, healthy = ClosableConnection(close_error=OSError("already gone")), ClosableConnection()
    tailscale._conns = {"w1": broken, "w2": healthy}

    await tailscale.close()

    assert healthy.closed and healthy.waited
    assert tailscale._conns == {}

  async def test_a_connection_that_fails_while_closing_does_not_fail_the_shutdown(self, tailscale):
    broken, healthy = ClosableConnection(wait_error=asyncssh.ConnectionLost("reset")), ClosableConnection()
    tailscale._conns = {"w1": broken, "w2": healthy}

    await tailscale.close()

    assert healthy.waited and tailscale._conns == {}

  async def test_with_no_connections_there_is_nothing_to_do(self, tailscale):
    await tailscale.close()

    assert tailscale._conns == {}


class TestNextOnline:
  @pytest.mark.parametrize("was_online", [True, False])
  def test_an_answer_makes_it_online_and_clears_the_misses(self, was_online):
    assert next_online(was_online, True, 5) == (True, 0)

  def test_one_miss_keeps_an_online_worker_online(self):
    assert next_online(True, False, 0) == (True, 1)

  def test_the_miss_that_reaches_the_limit_takes_it_offline(self):
    assert next_online(True, False, config.OFFLINE_AFTER_MISSED_PINGS - 1) == (False, config.OFFLINE_AFTER_MISSED_PINGS)

  def test_a_worker_that_is_offline_stays_offline_however_it_misses(self):
    assert next_online(False, False, 0) == (False, 1)


class FakePing:
  """The `tailscale ping` process: prints `output` and ends with `status`, or never ends."""

  def __init__(self, status=0, hangs=False, output=""):
    self.status = status
    self.hangs = hangs
    self.output = output
    self.killed = False
    self.returncode = None

  async def communicate(self):
    if self.hangs and not self.killed:
      await asyncio.sleep(3600)
    self.returncode = -9 if self.killed else self.status
    return self.output.encode(), None

  async def wait(self):
    return -9 if self.killed else self.status

  def kill(self):
    self.killed = True


class TestPing:
  @pytest.fixture
  def started(self, monkeypatch):
    commands = []

    def use(process):
      async def create(*command, **kwargs):
        commands.append(command)
        return process
      monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
      return commands

    return use

  def test_a_peer_that_answers_is_true(self, tailscale, started):
    commands = started(FakePing(0))
    assert asyncio.run(tailscale.ping("100.64.0.2")) is True
    assert commands[0][:3] == ("tailscale", "ping", "-c")
    assert commands[0][-1] == "100.64.0.2"

  def test_a_peer_that_does_not_answer_is_false(self, tailscale, started):
    started(FakePing(1))
    assert asyncio.run(tailscale.ping("100.64.0.2")) is False

  def test_how_the_answer_arrived_is_kept_for_that_peer(self, tailscale, started):
    started(FakePing(0, output="pong from w1 (100.64.0.2) via 203.0.113.5:41641 in 2ms\n"))
    asyncio.run(tailscale.ping("100.64.0.2"))
    assert tailscale.path("100.64.0.2") == "direct"
    assert tailscale.path("100.64.0.3") is None

  def test_the_latest_answer_replaces_the_one_before(self, tailscale, started):
    started(FakePing(0, output="pong from w1 (100.64.0.2) via DERP(blr) in 30ms\n"))
    asyncio.run(tailscale.ping("100.64.0.2"))
    assert tailscale.path("100.64.0.2") == "relay blr"
    started(FakePing(0, output="pong from w1 (100.64.0.2) via 203.0.113.5:41641 in 2ms\n"))
    asyncio.run(tailscale.ping("100.64.0.2"))
    assert tailscale.path("100.64.0.2") == "direct"

  def test_an_answer_that_does_not_say_how_it_arrived_still_counts_as_an_answer(self, tailscale, started):
    started(FakePing(0, output="something new\n"))
    assert asyncio.run(tailscale.ping("100.64.0.2")) is True
    assert tailscale.path("100.64.0.2") is None

  def test_the_output_of_a_ping_that_failed_is_not_trusted(self, tailscale, started):
    started(FakePing(1, output="pong from w1 (100.64.0.2) via DERP(blr) in 30ms\n"))
    assert asyncio.run(tailscale.ping("100.64.0.2")) is False
    assert tailscale.path("100.64.0.2") is None

  def test_a_peer_that_stops_answering_loses_its_path(self, tailscale, started):
    tailscale._paths["100.64.0.2"] = "direct"
    started(FakePing(1, output="ping did not respond\n"))
    asyncio.run(tailscale.ping("100.64.0.2"))
    assert tailscale.path("100.64.0.2") is None

  def test_a_ping_that_never_ends_loses_its_path_too(self, tailscale, started, monkeypatch):
    tailscale._paths["100.64.0.2"] = "direct"
    monkeypatch.setattr(config, "PING_TIMEOUT_SECONDS", -4.9)  # the wait is the timeout plus 5 s: 0.1 s
    started(FakePing(hangs=True))
    assert asyncio.run(tailscale.ping("100.64.0.2")) is False
    assert tailscale.path("100.64.0.2") is None

  def test_a_ping_that_never_ends_is_stopped_and_false(self, tailscale, started, monkeypatch):
    monkeypatch.setattr(config, "PING_TIMEOUT_SECONDS", -4.9)  # the wait is the timeout plus 5 s: 0.1 s
    process = FakePing(hangs=True)
    started(process)
    assert asyncio.run(tailscale.ping("100.64.0.2")) is False
    assert process.killed


class TestPingPath:
  @pytest.mark.parametrize("output, expected", [
      ("pong from w1 (100.64.0.2) via 203.0.113.5:41641 in 2ms", "direct"),
      ("pong from w1 (100.64.0.2) via [2001:db8::1]:41641 in 2ms", "direct"),
      ("pong from w1 (100.64.0.2) via DERP(blr) in 30ms", "relay blr"),
      ("pong from w1 (100.64.0.2) via DERP(us-east) in 30ms", "relay us-east"),
      ("pong from w1 (100.64.0.2) via peer-relay(10.0.0.1:1234) in 9ms", "relay"),
      ("some warning\npong from w1 (100.64.0.2) via DERP(nyc) in 30ms\n", "relay nyc"),
      ("", None),
      ("ping \"100.64.0.2\" did not respond", None),
  ])
  def test_table(self, output, expected):
    assert ping_path(output) == expected


class TestPeers:
  @pytest.fixture
  def peers(self, tailscale, monkeypatch):
    status = {"Peer": {
        "k1": {"HostName": "worker-1", "TailscaleIPs": ["100.64.0.2", "fd7a::2"]},
        "k2": {"HostName": "Worker-2", "TailscaleIPs": ["100.64.0.3"]},
        "k3": {"HostName": "pending", "TailscaleIPs": []},
        "k4": {"HostName": "no-addresses"},
    }}
    monkeypatch.setattr(tailscale, "status", lambda: status)
    return tailscale

  def test_every_machine_with_an_address_is_listed_with_its_first_address(self, peers):
    assert [(peer.hostname, peer.ip) for peer in peers.peers()] == [("worker-1", "100.64.0.2"), ("Worker-2", "100.64.0.3")]

  def test_a_tailnet_without_peers_lists_none(self, tailscale, monkeypatch):
    monkeypatch.setattr(tailscale, "status", lambda: {})
    assert tailscale.peers() == []

  @pytest.mark.parametrize("query", ["worker-2", "WORKER-2", " Worker-2 ", "100.64.0.3"])
  def test_a_machine_is_found_by_hostname_in_any_case_or_by_address(self, peers, query):
    assert peers.find_peer(query).hostname == "Worker-2"

  @pytest.mark.parametrize("query", ["worker-3", "100.64.0.9", "", "pending", "fd7a::2"])
  def test_a_machine_that_is_not_listed_is_not_found(self, peers, query):
    assert peers.find_peer(query) is None


class TestOnlineStates:
  @pytest.fixture
  def answers(self, tailscale, monkeypatch):
    """Which peers answer, by address; `connected` is whether the manager's own node is."""
    state = SimpleNamespace(up=set(), connected=True)

    async def ping(target):
      return target in state.up

    monkeypatch.setattr(tailscale, "ping", ping)
    monkeypatch.setattr(tailscale, "connected", lambda: state.connected)
    # The blocking check runs on the pool of the task that calls it.
    executor = ThreadPoolExecutor(max_workers=1)
    token = active_executor.set(executor)
    yield state
    active_executor.reset(token)
    executor.shutdown()

  def states(self, tailscale, **was):
    peers = {name: (f"100.64.0.{i}", online) for i, (name, online) in enumerate(was.items(), 1)}
    return asyncio.run(tailscale.online_states(peers))

  def test_peers_that_answer_are_online_at_once(self, tailscale, answers):
    answers.up = {"100.64.0.1", "100.64.0.2"}
    assert self.states(tailscale, a=False, b=True) == {"a": True, "b": True}

  def test_a_peer_that_missed_once_stays_online_and_the_second_miss_takes_it_out(self, tailscale, answers):
    assert self.states(tailscale, a=True) == {"a": True}
    assert self.states(tailscale, a=True) == {"a": False}

  def test_an_answer_between_misses_starts_the_count_again(self, tailscale, answers):
    self.states(tailscale, a=True)
    answers.up = {"100.64.0.1"}
    assert self.states(tailscale, a=True) == {"a": True}
    answers.up = set()
    assert self.states(tailscale, a=True) == {"a": True}

  def test_the_peers_are_counted_separately(self, tailscale, answers):
    answers.up = {"100.64.0.2"}
    self.states(tailscale, a=True, b=True)
    assert self.states(tailscale, a=True, b=True) == {"a": False, "b": True}

  def test_a_peer_that_is_no_longer_listed_starts_again_with_no_misses(self, tailscale, answers):
    self.states(tailscale, a=True)
    self.states(tailscale, b=True)
    assert self.states(tailscale, a=True) == {"a": True}

  def test_paths_are_kept_for_the_peers_asked_and_dropped_for_the_others(self, tailscale, answers):
    tailscale._paths = {"100.64.0.1": "direct", "100.64.0.9": "relay blr"}
    self.states(tailscale, a=True)
    assert tailscale.path("100.64.0.1") == "direct"
    assert tailscale.path("100.64.0.9") is None

  def test_when_the_manager_is_cut_off_nobody_changes_and_the_misses_are_not_counted(self, tailscale, answers):
    answers.connected = False
    assert self.states(tailscale, a=True, b=False) == {"a": True, "b": False}
    answers.connected = True
    assert self.states(tailscale, a=True) == {"a": True}

  def test_no_peers_is_no_states(self, tailscale, answers):
    assert asyncio.run(tailscale.online_states({})) == {}
