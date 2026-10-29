import asyncio
import random
import threading
import time
from datetime import datetime, timezone

import pytest

import config
from services.updates import Fact, Feed, Topic, Updates, UpdateSource


class Socket:
  """A browser's end of the connection: the frames it was sent, and optionally a slow or broken connection."""

  def __init__(self, delay=0.0):
    self.frames = []
    self.delay = delay
    self.gate = None
    self.broken = False

  async def emit(self, event, payload):
    if self.broken:
      raise RuntimeError("closed")
    if self.gate is not None:
      await self.gate.wait()
    if self.delay:
      await asyncio.sleep(self.delay)
    self.frames.append((event, payload))

  def states(self, topic):
    return [payload["data"] for event, payload in self.frames if event == "state" and payload["topic"] == topic]

  def errors(self):
    return [(payload["topic"], payload["status"]) for event, payload in self.frames if event == "topic_error"]


class Source:
  """The state of a topic, and what asking for it costs."""

  def __init__(self):
    self.value = 1
    self.loads = 0
    self.fail = False
    self.block = None

  def load(self):
    """Reads the value when called, and holds the first call while `block` is set."""
    self.loads += 1
    first = self.loads == 1
    value = self.value
    if self.block is not None and first:
      self.block.wait(5)
    if self.fail:
      raise RuntimeError("cannot load")
    return {"value": value}


class Table:
  """The rows of a feed, as their owner stores them before it publishes them."""

  def __init__(self):
    self.rows = []
    self.lock = threading.Lock()

  def add(self, **fields):
    with self.lock:
      row = {"id": len(self.rows) + 1, **fields}
      self.rows.append(row)
    return row


def ids(rows):
  return [row["id"] for row in rows]


async def until(condition, timeout=5):
  deadline = time.monotonic() + timeout
  while not condition():
    if time.monotonic() > deadline:
      raise AssertionError("timed out")
    await asyncio.sleep(0.005)


async def quiet(sockets, seconds=0.2, timeout=15):
  """Wait until none of the sockets has been sent anything for a while."""
  start = stamp = time.monotonic()
  last = None
  while True:
    now = [len(socket.frames) for socket in sockets]
    if now != last:
      last, stamp = now, time.monotonic()
    elif time.monotonic() - stamp >= seconds:
      return
    if time.monotonic() - start > timeout:
      raise AssertionError("the sockets never went quiet")
    await asyncio.sleep(0.02)


class World:
  def __init__(self, hub, sources, table, topics, connect):
    self.hub = hub
    self.sources = sources
    self.table = table
    self.topics = topics
    self.connect = connect


@pytest.fixture
def world(monkeypatch):
  monkeypatch.setattr(config, "UPDATES_COALESCE_SECONDS", 0.02)
  sources = {"a": Source(), "b": Source(), "c": Source()}
  table = Table()
  topics = {
      "f": Feed(),
      "a": Topic(sources["a"].load, frozenset({"x"})),
      "b": Topic(sources["b"].load, frozenset({"x", "y"})),
      "c": Topic(sources["c"].load, frozenset({"y"})),
  }
  hub = Updates()
  sids = []

  def connect(delay=0.0):
    socket = Socket(delay)
    sid = f"sid{len(sids)}"
    hub.connect(sid, socket.emit, topics)
    sids.append(sid)
    return socket, sid

  yield World(hub, sources, table, topics, connect)
  for sid in sids:
    hub.disconnect(sid)


class TestSubscribing:
  async def test_subscribing_sends_nothing(self, world):
    socket, sid = world.connect()
    world.hub.subscribe(sid, "a")
    await asyncio.sleep(0.1)
    assert socket.frames == []
    assert world.sources["a"].loads == 0

  async def test_an_unknown_topic_is_refused(self, world):
    socket, sid = world.connect()
    world.hub.subscribe(sid, "nothing")
    await until(lambda: socket.errors())
    assert socket.errors() == [("nothing", 404)]

  async def test_subscribing_again_does_not_follow_the_topic_twice(self, world):
    socket, sid = world.connect()
    world.hub.subscribe(sid, "a")
    world.hub.subscribe(sid, "a")
    world.hub.changed("x")
    await until(lambda: socket.states("a"))
    await asyncio.sleep(0.05)
    assert len(socket.states("a")) == 1

  async def test_a_topic_that_cannot_be_computed_is_skipped_and_sent_at_the_next_change(self, world):
    world.sources["a"].fail = True
    socket, sid = world.connect()
    world.hub.subscribe(sid, "a")
    world.hub.changed("x")
    await until(lambda: world.sources["a"].loads == 1)
    await asyncio.sleep(0.05)
    assert socket.frames == []
    world.sources["a"].fail = False
    world.hub.changed("x")
    await until(lambda: socket.states("a"))
    assert socket.states("a") == [{"value": 1}]

  async def test_the_state_is_encoded_as_the_rest_routes_encode_it(self, world):
    world.topics["a"] = Topic(lambda: {"at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)}, frozenset({"x"}))
    socket, sid = world.connect()
    world.hub.subscribe(sid, "a")
    world.hub.changed("x")
    await until(lambda: socket.states("a"))
    assert socket.states("a") == [{"at": "2026-01-02T03:04:05+00:00"}]


class TestChanges:
  async def test_a_change_of_a_source_sends_the_topics_that_depend_on_it(self, world):
    socket, sid = world.connect()
    for topic in ("a", "b", "c"):
      world.hub.subscribe(sid, topic)
    world.sources["a"].value = world.sources["b"].value = 2
    world.hub.changed("x")
    await until(lambda: socket.states("a") and socket.states("b"))
    await asyncio.sleep(0.05)
    assert socket.states("a") == [{"value": 2}] and socket.states("b") == [{"value": 2}]
    assert socket.states("c") == [] and world.sources["c"].loads == 0

  async def test_a_burst_of_changes_is_one_computation(self, world):
    socket, sid = world.connect()
    world.hub.subscribe(sid, "a")
    for value in range(2, 12):
      world.sources["a"].value = value
      world.hub.changed("x")
    await until(lambda: socket.states("a"))
    await asyncio.sleep(0.05)
    assert world.sources["a"].loads == 1
    assert socket.states("a") == [{"value": 11}]

  async def test_every_change_is_sent_even_when_the_state_is_the_same(self, world):
    socket, sid = world.connect()
    world.hub.subscribe(sid, "a")
    world.hub.changed("x")
    await until(lambda: len(socket.states("a")) == 1)
    world.hub.changed("x")
    await until(lambda: len(socket.states("a")) == 2)
    assert socket.states("a") == [{"value": 1}, {"value": 1}]

  async def test_a_topic_nobody_follows_is_not_computed(self, world):
    socket, sid = world.connect()
    world.hub.subscribe(sid, "a")
    world.hub.changed("y")
    await asyncio.sleep(0.1)
    assert world.sources["b"].loads == 0 and world.sources["c"].loads == 0
    assert socket.frames == []

  async def test_a_topic_is_still_sent_when_it_changes_while_a_feed_is_followed(self, world):
    socket, sid = world.connect()
    for topic in ("f", "a"):
      world.hub.subscribe(sid, topic)
    world.sources["a"].value = 2
    world.hub.changed("x")
    await until(lambda: socket.states("a"))
    assert socket.states("a") == [{"value": 2}] and socket.states("f") == []

  async def test_changes_can_be_reported_from_other_threads(self, world):
    socket, sid = world.connect()
    world.hub.subscribe(sid, "a")

    def change(value):
      world.sources["a"].value = value
      world.hub.changed("x")

    threads = [threading.Thread(target=change, args=(value,)) for value in range(2, 8)]
    for thread in threads:
      thread.start()
    for thread in threads:
      thread.join()
    await until(lambda: socket.states("a") and socket.states("a")[-1] == {"value": world.sources["a"].value})

  async def test_a_change_before_anyone_is_connected_does_nothing(self, monkeypatch):
    monkeypatch.setattr(config, "UPDATES_COALESCE_SECONDS", 0.02)
    Updates().changed("x")


class TestLeaving:
  async def test_an_unsubscribed_topic_is_no_longer_sent(self, world):
    socket, sid = world.connect()
    world.hub.subscribe(sid, "a")
    world.hub.unsubscribe(sid, "a")
    world.hub.changed("x")
    await asyncio.sleep(0.1)
    assert socket.frames == []

  async def test_a_disconnected_connection_is_no_longer_sent_anything(self, world):
    gone, gone_sid = world.connect()
    stays, stays_sid = world.connect()
    for sid in (gone_sid, stays_sid):
      world.hub.subscribe(sid, "a")
    world.hub.disconnect(gone_sid)
    world.hub.changed("x")
    await until(lambda: stays.states("a"))
    assert gone.frames == []

  async def test_a_topic_is_not_computed_once_its_last_follower_has_gone(self, world):
    socket, sid = world.connect()
    world.hub.subscribe(sid, "a")
    world.hub.disconnect(sid)
    world.hub.changed("x")
    await asyncio.sleep(0.1)
    assert world.sources["a"].loads == 0
    assert world.hub._subscribers.get("a") == set()

  async def test_a_connection_that_cannot_be_written_to_does_not_hold_up_the_others(self, world):
    broken, broken_sid = world.connect()
    healthy, healthy_sid = world.connect()
    broken.broken = True
    for sid in (broken_sid, healthy_sid):
      world.hub.subscribe(sid, "a")
    world.hub.changed("x")
    await until(lambda: healthy.states("a"))
    world.sources["a"].value = 2
    world.hub.changed("x")
    await until(lambda: len(healthy.states("a")) == 2)


class TestSlowConnections:
  async def test_a_slow_connection_skips_states_and_ends_on_the_newest(self, world):
    socket, sid = world.connect()
    socket.gate = asyncio.Event()
    world.hub.subscribe(sid, "a")
    for value in range(2, 6):
      world.sources["a"].value = value
      world.hub.changed("x")
      await asyncio.sleep(0.05)
    socket.gate.set()
    await until(lambda: socket.states("a") and socket.states("a")[-1] == {"value": 5})
    assert socket.states("a") == [{"value": 2}, {"value": 5}], "the states between were replaced, not queued"

  async def test_a_state_computed_earlier_never_overtakes_one_computed_later(self, world):
    socket, sid = world.connect()
    world.sources["a"].block = threading.Event()
    world.hub.subscribe(sid, "a")
    world.hub.changed("x")
    await asyncio.sleep(0.1)
    world.sources["a"].value = 2
    world.hub.changed("x")
    await asyncio.sleep(0.1)
    world.sources["a"].block.set()
    await until(lambda: socket.states("a") and socket.states("a")[-1] == {"value": 2})
    values = [state["value"] for state in socket.states("a")]
    assert values == sorted(values)
    assert world.sources["a"].loads == 2, "the change made while it was computed is computed once more"


class TestAgainstAModel:
  """Many connections following and leaving topics while their sources change from the loop and from threads, some
  connections slow: whatever happens, once every source has changed one last time every connection holds the current
  state of each topic it follows, and nothing arrives for a topic it left."""

  @pytest.mark.parametrize("seed", range(8))
  async def test_every_follower_ends_on_the_current_state(self, world, seed):
    rng = random.Random(seed)
    clients = [world.connect(delay=rng.choice([0, 0, 0.003, 0.01])) for _ in range(4)]
    following = [set() for _ in clients]
    deps = {"a": ["x"], "b": ["x", "y"], "c": ["y"]}
    threads = []

    def change(name):
      world.sources[name].value = rng.randint(0, 5)
      world.hub.changed(rng.choice(deps[name]))

    for _ in range(200):
      index = rng.randrange(len(clients))
      socket, sid = clients[index]
      action = rng.choices(["subscribe", "unsubscribe", "change", "change from a thread", "signal only"], [3, 2, 5, 2, 1])[0]
      topic = rng.choice("abc")
      if action == "subscribe":
        world.hub.subscribe(sid, topic)
        following[index].add(topic)
      elif action == "unsubscribe":
        world.hub.unsubscribe(sid, topic)
        following[index].discard(topic)
      elif action == "change":
        change(topic)
      elif action == "change from a thread":
        thread = threading.Thread(target=change, args=(topic,))
        thread.start()
        threads.append(thread)
      else:
        world.hub.changed(rng.choice(deps[topic]))
      await asyncio.sleep(rng.choice([0, 0, 0.001, 0.005, 0.02]))
    for thread in threads:
      thread.join()

    sockets = [socket for socket, _ in clients]
    await quiet(sockets)
    counts = [[len(socket.states(topic)) for topic in "abc"] for socket in sockets]
    world.hub.changed("x")
    world.hub.changed("y")
    await quiet(sockets)

    for index, socket in enumerate(sockets):
      for topic_index, topic in enumerate("abc"):
        states = socket.states(topic)
        if topic in following[index]:
          assert states, f"sid {index} follows {topic} and received nothing"
          assert states[-1] == {"value": world.sources[topic].value}, f"sid {index} is stale on {topic}"
        else:
          assert len(states) == counts[index][topic_index], f"sid {index} was sent {topic} after it left"


class TestFeeds:
  async def test_subscribing_to_a_feed_sends_nothing(self, world):
    world.table.add()
    socket, sid = world.connect()
    world.hub.subscribe(sid, "f")
    await asyncio.sleep(0.1)
    assert socket.frames == []

  async def test_a_published_row_reaches_each_subscriber_alone_and_nobody_else(self, world):
    first, first_sid = world.connect()
    second, second_sid = world.connect()
    other, other_sid = world.connect()
    for sid in (first_sid, second_sid):
      world.hub.subscribe(sid, "f")
    world.hub.subscribe(other_sid, "a")
    world.hub.publish("f", [world.table.add(text="deployed")])
    await until(lambda: first.states("f") and second.states("f"))
    await asyncio.sleep(0.05)
    assert first.states("f") == [[{"id": 1, "text": "deployed"}]] == second.states("f")
    assert other.frames == []

  async def test_a_change_of_a_source_does_not_touch_a_feed(self, world):
    socket, sid = world.connect()
    world.hub.subscribe(sid, "f")
    world.hub.changed("x")
    world.hub.changed("y")
    await asyncio.sleep(0.1)
    assert socket.frames == []

  async def test_a_row_is_sent_as_the_rest_routes_encode_it(self, world):
    socket, sid = world.connect()
    world.hub.subscribe(sid, "f")
    world.hub.publish("f", [{"at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)}])
    await until(lambda: socket.states("f"))
    assert socket.states("f") == [[{"at": "2026-01-02T03:04:05+00:00"}]]

  async def test_rows_are_sent_in_the_order_they_were_published_and_none_replaces_another(self, world):
    socket, sid = world.connect()
    socket.gate = asyncio.Event()
    world.hub.subscribe(sid, "f")
    for _ in range(6):
      world.hub.publish("f", [world.table.add()])
    await asyncio.sleep(0.05)
    socket.gate.set()
    await until(lambda: len(socket.states("f")) == 6)
    assert [ids(rows) for rows in socket.states("f")] == [[1], [2], [3], [4], [5], [6]]

  async def test_rows_can_be_published_from_other_threads(self, world):
    socket, sid = world.connect()
    world.hub.subscribe(sid, "f")
    threads = [threading.Thread(target=lambda: world.hub.publish("f", [world.table.add()])) for _ in range(20)]
    for thread in threads:
      thread.start()
    for thread in threads:
      thread.join()
    await until(lambda: len(socket.states("f")) == 20)
    assert sorted(row_id for rows in socket.states("f") for row_id in ids(rows)) == list(range(1, 21))

  async def test_a_row_published_before_anyone_is_connected_does_nothing(self):
    Updates().publish("f", [{"id": 1}])

  async def test_a_row_is_not_sent_to_a_connection_that_has_gone(self, world):
    gone, gone_sid = world.connect()
    stays, stays_sid = world.connect()
    for sid in (gone_sid, stays_sid):
      world.hub.subscribe(sid, "f")
    world.hub.disconnect(gone_sid)
    world.hub.publish("f", [world.table.add()])
    await until(lambda: stays.states("f"))
    assert gone.frames == []

  async def test_an_unsubscribed_feed_is_sent_nothing_more_not_even_what_was_waiting(self, world):
    socket, sid = world.connect()
    socket.gate = asyncio.Event()
    world.hub.subscribe(sid, "a")
    world.hub.subscribe(sid, "f")
    world.hub.changed("x")
    await asyncio.sleep(0.1)
    for _ in range(3):
      world.hub.publish("f", [world.table.add()])
    await asyncio.sleep(0.05)
    world.hub.unsubscribe(sid, "f")
    world.hub.publish("f", [world.table.add()])
    socket.gate.set()
    await until(lambda: socket.states("a"))
    await asyncio.sleep(0.1)
    assert socket.states("f") == []


class TestFeedsAgainstAModel:
  """Many connections following and leaving a feed while rows are stored and published from the loop and from threads, some
  connections slow: whatever happens, once it is quiet every connection that follows has received each row stored after
  it last subscribed, no row twice, and a connection that left was sent nothing after."""

  @pytest.mark.parametrize("seed", range(8))
  async def test_every_follower_receives_every_row_stored_after_it_subscribed_once(self, world, seed):
    rng = random.Random(seed)
    clients = [world.connect(delay=rng.choice([0, 0, 0.003, 0.01])) for _ in range(4)]
    following = [False] * len(clients)
    stored_before = [0] * len(clients)
    threads = []

    def publish():
      world.hub.publish("f", [world.table.add()])

    for _ in range(200):
      index = rng.randrange(len(clients))
      sid = clients[index][1]
      action = rng.choices(["subscribe", "unsubscribe", "publish", "publish from a thread"], [3, 2, 6, 3])[0]
      if action == "subscribe":
        stored_before[index] = len(world.table.rows)
        world.hub.subscribe(sid, "f")
        following[index] = True
      elif action == "unsubscribe":
        world.hub.unsubscribe(sid, "f")
        following[index] = False
      elif action == "publish":
        publish()
      else:
        thread = threading.Thread(target=publish)
        thread.start()
        threads.append(thread)
      await asyncio.sleep(rng.choice([0, 0, 0.001, 0.005, 0.02]))
    for thread in threads:
      thread.join()

    sockets = [socket for socket, _ in clients]
    await quiet(sockets)
    counts = [len(socket.states("f")) for socket in sockets]
    for _ in range(5):
      publish()
    await quiet(sockets)

    for index, socket in enumerate(sockets):
      received = [row_id for rows in socket.states("f") for row_id in ids(rows)]
      assert len(received) == len(set(received)), f"sid {index} was sent a row twice"
      if following[index]:
        stored = set(range(stored_before[index] + 1, len(world.table.rows) + 1))
        assert stored <= set(received), f"sid {index} missed {sorted(stored - set(received))}"
      else:
        assert len(socket.states("f")) == counts[index], f"sid {index} was sent rows after it left"


class TestFact:
  class Value:
    """A value that changes, and what asking for it costs."""

    def __init__(self, value=1, delay=0.0):
      self.value = value
      self.delay = delay
      self.computed = 0

    def __call__(self):
      self.computed += 1
      value = self.value
      if self.delay:
        time.sleep(random.random() * self.delay)
      return value

  @pytest.fixture
  def sources(self, monkeypatch):
    fired = []
    monkeypatch.setattr(Updates, "changed", lambda self, source: fired.append(source))
    return fired

  def test_the_first_check_fires_whatever_the_value_is(self, sources):
    for value in (1, None, (), {}, ""):
      fact = Fact(lambda value=value: value, UpdateSource.TASKS)
      fact.check()
    assert sources == [UpdateSource.TASKS] * 5

  def test_a_value_that_is_the_same_fires_nothing(self, sources):
    value = self.Value()
    fact = Fact(value, UpdateSource.TASKS)
    fact.check()
    fact.check()
    assert sources == [UpdateSource.TASKS] and value.computed == 2

  def test_a_value_that_differs_fires_once_each_time(self, sources):
    value = self.Value()
    fact = Fact(value, UpdateSource.TASKS)
    fact.check()
    value.value = 2
    fact.check()
    fact.check()
    value.value = 1
    fact.check()
    assert sources == [UpdateSource.TASKS] * 3

  def test_the_callback_is_called_when_it_differs_and_not_otherwise(self, sources):
    value = self.Value()
    called = []
    fact = Fact(value, on_change=called.append)
    fact.check()
    fact.check()
    value.value = 2
    fact.check()
    assert called == [1, 2] and sources == []

  def test_it_fires_its_source_and_calls_its_callback(self, sources):
    called = []
    Fact(lambda: 1, UpdateSource.RESTORING, on_change=called.append).check()
    assert sources == [UpdateSource.RESTORING] and called == [1]

  def test_the_values_reach_the_callback_in_the_order_they_were_worked_out(self, sources):
    entered, release = threading.Event(), threading.Event()
    state = {"value": 1}
    given = []

    def on_change(value):
      if value == 1:
        entered.set()
        release.wait(5)
      given.append(value)

    fact = Fact(lambda: state["value"], on_change=on_change)
    slow = threading.Thread(target=fact.check)
    slow.start()
    entered.wait(5)
    state["value"] = 2
    fast = threading.Thread(target=fact.check)
    fast.start()
    time.sleep(0.05)
    release.set()
    slow.join()
    fast.join()
    assert given == [1, 2] and fact.last == 2

  def test_a_source_is_fired_after_the_callback(self, sources):
    order = []
    Fact(lambda: 1, UpdateSource.TASKS, on_change=lambda value: order.append(("callback", list(sources)))).check()
    assert order == [("callback", [])] and sources == [UpdateSource.TASKS]

  def test_it_remembers_the_last_value(self, sources):
    value = self.Value(3)
    fact = Fact(value)
    fact.check()
    assert fact.last == 3

  def test_a_slow_check_does_not_leave_an_older_value_over_a_newer_one(self, sources):
    started, release = threading.Event(), threading.Event()
    state = {"value": 1, "calls": 0}

    def compute():
      value = state["value"]
      state["calls"] += 1
      if state["calls"] == 1:
        started.set()
        release.wait(5)
      return value

    fact = Fact(compute, UpdateSource.TASKS)
    slow = threading.Thread(target=fact.check)
    slow.start()
    started.wait(5)
    state["value"] = 2
    fast = threading.Thread(target=fact.check)
    fast.start()
    time.sleep(0.05)
    release.set()
    slow.join()
    fast.join()

    assert fact.last == 2
