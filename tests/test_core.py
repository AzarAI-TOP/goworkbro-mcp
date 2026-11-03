"""Hermetic tests for the BLE transport: no phone, no radio.

The bugs these lock down were all found on real hardware on 2026-11-03:

* two tool calls in parallel shared one pending-reply slot and swapped answers
  (the agent went on to mark the WRONG todo done);
* a lost reply made the client silently re-send a mutation.
"""

import asyncio
import json
import struct

import pytest

from goworkbro_mcp import core

TODO_A = "b558a580-6b26-461b-a48e-2ba84200bfc9"
TODO_B = "92bd3d60-eac7-419e-a4e1-b501feb96b7f"
TODO_C = "b5aa0000-0000-0000-0000-000000000000"


class FakeClient:
    """A phone that speaks the framing but only answers what it is told to."""

    def __init__(self, handler, mtu_size: int | None = None) -> None:
        self.handler = handler
        self.buf = bytearray()
        self.notify = None
        self.is_connected = True
        self.sent: list[dict] = []
        self.stale: list[dict] = []
        if mtu_size is not None:
            self.mtu_size = mtu_size

    async def start_notify(self, _uuid, callback) -> None:
        self.notify = callback

    async def stop_notify(self, _uuid) -> None:
        self.notify = None

    async def read_gatt_char(self, _uuid) -> bytes:
        return json.dumps({"app": "GoWorkBro", "protocol": 1}).encode()

    async def write_gatt_char(self, _uuid, data, response: bool = False) -> None:
        # A reply that arrived too late shows up now, in front of this request's
        # own answer — exactly what desynchronised the real link.
        while self.stale:
            await self._emit(self.stale.pop(0))
        self.buf += bytes(data)
        while len(self.buf) >= 4:
            (length,) = struct.unpack(">I", bytes(self.buf[:4]))
            if len(self.buf) - 4 < length:
                return
            body = bytes(self.buf[4:4 + length])
            del self.buf[:4 + length]
            request = json.loads(body)
            self.sent.append(request)
            reply = self.handler(request)
            if reply is not None:
                await self._emit(reply)

    async def _emit(self, reply: dict) -> None:
        if self.notify is not None:
            self.notify(None, bytearray(core.frame(reply)))

    async def disconnect(self) -> None:
        self.is_connected = False


def ok(op: str, **data) -> dict:
    return {"ok": True, "op": op, "data": data}


def run(coro):
    return asyncio.run(coro)


async def opened(handler, **kwargs) -> core.Link:
    link = core.Link(FakeClient(handler, **kwargs), "token")
    await link.open()
    return link


# ---- id resolution ---------------------------------------------------------


def test_full_uuid_skips_the_list_round_trip():
    def handler(request):
        raise AssertionError("no request expected")

    async def scenario():
        link = await opened(handler)
        assert await core.resolve_id(link, "todos", TODO_A) == TODO_A
        assert link.client.sent == []

    run(scenario())


def test_prefix_resolves_against_the_list():
    def handler(request):
        assert request["op"] == "todos.list"
        return ok("todos.list", items=[{"id": TODO_A}, {"id": TODO_B}, {"id": TODO_C}])

    async def scenario():
        link = await opened(handler)
        assert await core.resolve_id(link, "todos", "92bd3d60") == TODO_B
        with pytest.raises(core.GwbError) as ambiguous:
            await core.resolve_id(link, "todos", "b5")  # TODO_A and TODO_C both
        assert ambiguous.value.code == "ambiguous"
        with pytest.raises(core.GwbError) as missing:
            await core.resolve_id(link, "todos", "ffff")
        assert missing.value.code == "not_found"

    run(scenario())


# ---- reply/frame handling --------------------------------------------------


def test_a_crossed_reply_is_refused_not_used():
    def handler(_request):
        return ok("todos.list", items=[])  # the wrong op for every request

    async def scenario():
        link = await opened(handler)
        with pytest.raises(core.GwbError) as desync:
            await link.request("ping")
        assert desync.value.code == "desync"

    run(scenario())


def test_a_late_reply_cannot_be_handed_to_the_next_request(monkeypatch):
    monkeypatch.setattr(core, "REQUEST_TIMEOUT", 0.05)

    def handler(_request):
        return None  # the first reply never comes

    async def scenario():
        link = await opened(handler)
        with pytest.raises(asyncio.TimeoutError):
            await link.request("ping")
        # That answer finally lands while the NEXT request is in flight; it
        # must not be mistaken for the next one's answer.
        link.client.stale.append(ok("ping", protocol=1, today="2026-11-03"))
        link.client.handler = lambda _request: ok("todos.list", items=[])
        with pytest.raises(core.GwbError) as desync:
            await link.request("todos.list")
        assert desync.value.code == "desync"

    run(scenario())


def test_mtu_is_negotiated_into_a_bigger_chunk():
    def handler(_request):
        return ok("ping", protocol=1)

    async def scenario():
        link = await opened(handler, mtu_size=247)
        assert (link.mtu, link.chunk) == (247, 244)
        fallback = await opened(handler)  # stack that never reports an MTU
        assert (fallback.mtu, fallback.chunk) == (core.ATT_FLOOR, 20)

    run(scenario())


# ---- retry policy ----------------------------------------------------------


@pytest.fixture
def reconnect(monkeypatch):
    """Give `Phone` a fresh scripted link on every connect, and count them."""
    monkeypatch.setattr(core, "REQUEST_TIMEOUT", 0.05)
    links: list[FakeClient] = []

    def install(*handlers, mtu_size=None):
        async def fake_connect(_timeout: float = 12.0) -> core.Link:
            client = FakeClient(handlers[len(links)], mtu_size=mtu_size)
            links.append(client)
            link = core.Link(client, "token")
            await link.open()
            return link

        monkeypatch.setattr(core, "connect", fake_connect)
        return links

    return install


def test_replayable_add_is_retried_with_the_same_id(reconnect):
    identifiers = []

    def first(request):
        identifiers.append(request["data"]["id"])
        return None  # reply lost

    def second(request):
        identifiers.append(request["data"]["id"])
        return ok("todo.add", id=request["data"]["id"], created=True)

    links = reconnect(first, second)

    async def scenario():
        result = await core.Phone().call("todo.add", {"id": TODO_A, "title": "写毕设"})
        assert result["id"] == TODO_A

    run(scenario())
    assert len(links) == 2  # a real reconnect, not a reused broken link
    assert identifiers == [TODO_A, TODO_A]  # same id → the phone upserts


def test_non_replayable_write_is_not_sent_twice(reconnect):
    sent = []

    def handler(request):
        sent.append(request["op"])
        return None  # reply lost

    links = reconnect(handler)

    async def scenario():
        with pytest.raises(core.GwbError) as lost:
            await core.Phone().call("habit.check", {"id": TODO_A})
        assert lost.value.code == "link_lost"

    run(scenario())
    assert sent == ["habit.check"]  # never replayed: the count could double
    assert len(links) == 1


def test_concurrent_calls_never_interleave_on_the_wire(reconnect):
    """Two tool calls at once: each answer must go to its own caller."""

    def handler(request):
        op = request["op"]
        if op == "todos.list":
            return ok("todos.list", items=[{"id": TODO_A}])
        if op == "countdowns.list":
            return ok("countdowns.list", items=[{"target_at": "2026-11-05T14:00:00"}])
        if op == "todo.done":
            return ok("todo.done", id=TODO_A, is_done=True, done_date="2026-11-03")
        raise AssertionError(op)

    reconnect(handler)

    async def scenario():
        phone = core.Phone()
        return await asyncio.gather(
            phone.call("todos.list"),
            phone.call("countdowns.list"),
            phone.call_id("todo.done", "todos", TODO_A, {"done": True}),
        )

    todos, countdowns, done = run(scenario())
    assert todos["items"][0]["id"] == TODO_A
    assert countdowns["items"][0]["target_at"] == "2026-11-05T14:00:00"
    assert done["is_done"] is True


def test_mtu_exchange_is_asked_on_the_backend(monkeypatch):
    def handler(_request):
        return ok("ping", protocol=1)

    class Backend:
        def __init__(self):
            self.asked = 0

        async def _acquire_mtu(self):
            self.asked += 1

    async def scenario():
        link = core.Link(FakeClient(handler, mtu_size=23), "token")
        link.client._backend = Backend()
        link.client.mtu_size = 23
        assert await core.acquire_mtu(link.client) == 23  # answer unchanged…
        assert link.client._backend.asked == 1  # …but we did ask

        monkeypatch.setenv("GWB_MTU", "off")
        assert await core.acquire_mtu(link.client) == 23
        assert link.client._backend.asked == 1  # escape hatch: not asked again

    run(scenario())


def test_v2_capability_gate_asks_once_and_refuses_an_old_phone(reconnect):
    pings = []

    def handler(request):
        pings.append(request["op"])
        return ok("ping", protocol=1, today="2026-11-03")  # a pre-3.8 phone

    reconnect(handler)

    async def scenario():
        phone = core.Phone()
        assert await phone.require(1) == 1
        with pytest.raises(core.GwbError) as too_old:
            await phone.require(2)
        assert too_old.value.code == "too_old"
        with pytest.raises(core.GwbError):  # still refused, no second ping
            await phone.require(2)

    run(scenario())
    assert pings == ["ping"]  # the version is cached for the life of the link
