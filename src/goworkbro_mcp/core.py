"""Transport + protocol glue for GoWorkBro 电脑协同 (BLE), shared by the CLI and the MCP server.

手机是唯一数据源：这里只发命令、收结果，没有双向同步。
协议见 docs/remote-protocol.md。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import struct
import time
import uuid
import warnings
from pathlib import Path
from typing import Any

from bleak import BleakClient, BleakScanner

# bleak on Linux/BlueZ never learns the negotiated MTU and warns when asked; the
# ATT floor is a valid (if not maximal) write size, so the noise is not useful.
warnings.filterwarnings("ignore", message="Using default MTU value")

SERVICE = "5c0de000-9a3e-4f6b-b1d2-7e8f4a6c2d10"
INFO = "5c0de001-9a3e-4f6b-b1d2-7e8f4a6c2d10"
UP = "5c0de002-9a3e-4f6b-b1d2-7e8f4a6c2d10"
DOWN = "5c0de003-9a3e-4f6b-b1d2-7e8f4a6c2d10"

CONFIG = Path(os.environ.get("GWB_CONFIG") or Path.home() / ".config" / "gwb" / "config.json")
MAX_ATTRIBUTE = 512
ATT_FLOOR = 23

# Ops whose replay is harmless: reads, and writes the caller made replay-safe
# by supplying the id itself (the phone upserts on an existing id).
READ_OPS = frozenset({"ping", "todos.list", "habits.list", "countdowns.list"})

# One request's budget; a hung phone must not hold a tool call hostage.
REQUEST_TIMEOUT = 6.0

# Transport faults after which the pending reply slot can no longer be trusted.
RESYNC_CODES = frozenset({"desync", "bad_frame"})
TRANSPORT_ERRORS = (asyncio.TimeoutError, OSError, EOFError)


class GwbError(Exception):
    """The phone answered with ok:false, or the link died."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


# ---- token -----------------------------------------------------------------


def load_token() -> str:
    token = os.environ.get("GWB_TOKEN")
    if token:
        return token
    try:
        return json.loads(CONFIG.read_text())["token"]
    except (OSError, KeyError, json.JSONDecodeError):
        raise GwbError("no_token", f"还没有 token：gwb token <设置 → 电脑协同 里的那串>（会写到 {CONFIG}）")


def save_token(token: str) -> Path:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if CONFIG.exists():
        try:
            data = json.loads(CONFIG.read_text())
        except json.JSONDecodeError:
            data = {}
    data["token"] = token
    CONFIG.write_text(json.dumps(data, indent=2) + "\n")
    CONFIG.chmod(0o600)
    return CONFIG


# ---- framing (mirror of core/remote/Framing.kt) -----------------------------


def frame(obj: dict) -> bytes:
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(body)) + body


class Reassembler:
    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, chunk: bytes) -> list[str]:
        self.buf += chunk
        out = []
        while len(self.buf) >= 4:
            (length,) = struct.unpack(">I", bytes(self.buf[:4]))
            if length < 1 or length > 64 * 1024:
                self.buf.clear()
                raise GwbError("bad_frame", f"帧长度不合法：{length}")
            if len(self.buf) - 4 < length:
                break
            out.append(bytes(self.buf[4:4 + length]).decode("utf-8"))
            del self.buf[:4 + length]
        return out


# ---- link ------------------------------------------------------------------


class Link:
    """One request at a time; each waits for its reply frame on DOWN.

    The wire has no request id, so a reply is matched to its request by the
    `op` it echoes. A mismatch means the pending slot is out of step with the
    phone (a late frame from an earlier request); the caller must throw the
    link away — [Phone] does exactly that.
    """

    def __init__(self, client: BleakClient, token: str) -> None:
        self.client = client
        self.token = token
        self.reader = Reassembler()
        self.pending: asyncio.Future | None = None
        self.mtu = ATT_FLOOR
        self.chunk = self._chunk_for(self.mtu)

    @staticmethod
    def _chunk_for(mtu: int) -> int:
        return max(20, min(MAX_ATTRIBUTE, mtu - 3))

    async def open(self) -> None:
        await self.client.start_notify(DOWN, self._on_notify)
        info = json.loads((await self.client.read_gatt_char(INFO)).decode("utf-8"))
        if info.get("app") != "GoWorkBro":
            raise GwbError("not_goworkbro", f"对面不是 GoWorkBro：{info}")
        # BlueZ never learns the negotiated MTU on its own; asking makes bleak
        # perform the exchange, and the phone picks the value up in
        # onMtuChanged. Without this both directions stay at the 23-byte floor
        # and every frame is cut into 20-byte chunks.
        self.mtu = await acquire_mtu(self.client)
        self.chunk = self._chunk_for(self.mtu)

    def _on_notify(self, _char, data: bytearray) -> None:
        for text in self.reader.feed(bytes(data)):
            if self.pending is not None and not self.pending.done():
                self.pending.set_result(json.loads(text))

    async def request(self, op: str, data: dict | None = None, timeout: float | None = None) -> dict:
        timeout = REQUEST_TIMEOUT if timeout is None else timeout
        payload = {"v": 1, "token": self.token, "op": op}
        if data:
            payload["data"] = data
        self.pending = asyncio.get_running_loop().create_future()
        try:
            body = frame(payload)
            for start in range(0, len(body), self.chunk):
                await self.client.write_gatt_char(
                    UP, body[start:start + self.chunk], response=True)
            response = await asyncio.wait_for(self.pending, timeout)
        finally:
            # Whatever went wrong, this slot is spent: never let a late frame
            # be handed to the next request.
            self.pending = None
        echoed = response.get("op")
        if echoed not in (None, op):
            raise GwbError("desync", f"回复对不上：要的是 {op}，收到 {echoed}")
        return response

    async def close(self) -> None:
        self.reader.buf.clear()
        try:
            await self.client.stop_notify(DOWN)
        except Exception:
            pass
        try:
            await self.client.disconnect()
        except Exception:
            pass

    @property
    def connected(self) -> bool:
        return bool(self.client.is_connected)


async def find_phone(timeout: float = 12.0):
    """Scans for our service UUID. The address rotates, so never cache a MAC."""

    def match(device, adv) -> bool:
        return SERVICE.lower() in [u.lower() for u in adv.service_uuids]

    device = await BleakScanner.find_device_by_filter(match, timeout=timeout)
    if device is None:
        raise GwbError(
            "not_found",
            "没扫到手机。检查：手机蓝牙开着？设置 → 电脑协同 开着？距离够近？",
        )
    return device


async def acquire_mtu(client: BleakClient) -> int:
    """Ask BlueZ for the real MTU; fall back to the ATT floor.

    Without this both directions stay at 23 bytes per attribute and every
    frame is cut into 20-byte chunks (a 3.7 KB todo list = 180 notifications).
    The exchange hides on the backend, not on BleakClient, and is BlueZ-only;
    any other stack just reports its own `mtu_size` (or 23).

    `GWB_MTU=off` skips it, in case a future phone dislikes the exchange.
    """
    if os.environ.get("GWB_MTU", "").strip().lower() in ("off", "0", "no"):
        return ATT_FLOOR
    acquire = getattr(getattr(client, "_backend", None), "_acquire_mtu", None)
    if acquire is not None:
        try:
            await acquire()
        except Exception:
            pass
    try:
        return int(getattr(client, "mtu_size", ATT_FLOOR) or ATT_FLOOR)
    except Exception:
        return ATT_FLOOR


async def open_client(timeout: float = 12.0) -> tuple[BleakClient, Any]:
    """Scan, connect, and survive a stale link left by a killed client."""
    device = await find_phone(timeout)
    client = BleakClient(device, timeout=25)
    try:
        await client.connect()
    except Exception:
        # A client that died mid-session can leave BlueZ holding an LE link
        # that blocks a new one; kick the peer away and try once more.
        try:
            await client.disconnect()
        except Exception:
            pass
        device = await find_phone(timeout)
        client = BleakClient(device, timeout=25)
        await client.connect()
    if not client.is_connected:
        raise GwbError("not_connected", "连上了但没建立会话，再试一次？")
    return client, device


async def connect(timeout: float = 12.0) -> Link:
    client, _device = await open_client(timeout)
    link = Link(client, load_token())
    await link.open()
    return link


# ---- convenience -----------------------------------------------------------


LIST_OPS = {
    "todos": "todos.list",
    "habits": "habits.list",
    "countdowns": "countdowns.list",
}


def check(response: dict) -> dict:
    if not response.get("ok"):
        raise GwbError(response.get("error", "unknown"), response.get("message", ""))
    return response.get("data", {})


async def list_items(link: Link, kind: str) -> list:
    return check(await link.request(LIST_OPS[kind])).get("items", [])


UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def new_id() -> str:
    """A client-side id, so an add can be replayed without duplicating."""
    return str(uuid.uuid4())


def is_replayable(op: str, data: dict | None) -> bool:
    """Would sending this request twice be indistinguishable from once?"""
    if op in READ_OPS:
        return True
    return op.endswith(".add") and bool(data and data.get("id"))


async def resolve_id(link: Link, kind: str, prefix: str) -> str:
    """A full id, or the unique prefix of one (ids are UUIDs — nobody types them)."""
    if UUID_RE.match(prefix):
        return prefix  # already exact: skip a whole round trip
    hits = [i["id"] for i in await list_items(link, kind) if i["id"].startswith(prefix)]
    if not hits:
        raise GwbError("not_found", f"没找到 id 以 {prefix} 开头的条目")
    if len(hits) > 1:
        raise GwbError("ambiguous", f"{prefix} 有歧义：{', '.join(h[:8] for h in hits)}")
    return hits[0]


class Session:
    """`async with Session() as link:` — scan, connect, and always hang up."""

    def __init__(self, timeout: float = 12.0) -> None:
        self.timeout = timeout
        self.link: Link | None = None

    async def __aenter__(self) -> Link:
        self.link = await connect(self.timeout)
        return self.link

    async def __aexit__(self, *exc) -> None:
        if self.link is not None:
            await self.link.close()


class Phone:
    """A link that survives between MCP tool calls.

    A scan costs ~3 s, so keeping the connection is the difference between a
    snappy agent and a 4 s tax per tool call. The link is dropped after
    [IDLE_CLOSE] seconds of silence (BLE connected state costs phone battery).

    Everything that touches the wire runs under ONE lock — including id
    resolution. Two tool calls in flight at once (agents do fire them in
    parallel) would otherwise share the single pending-reply slot and swap
    each other's answers.
    """

    IDLE_CLOSE = 300.0

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._link: Link | None = None
        self._last = 0.0

    async def link(self) -> Link:
        """The live link — for callers that issue several requests themselves."""
        async with self._lock:
            await self._ensure()
            return self._link  # type: ignore[return-value]

    async def call(self, op: str, data: dict | None = None) -> dict:
        async with self._lock:
            await self._ensure()
            return await self._request(op, data)

    async def call_id(
        self, op: str, kind: str, prefix: str, data: dict | None = None,
    ) -> dict:
        """Resolve a short id and send the op in ONE locked turn.

        Resolving outside the lock is what let two concurrent tool calls
        interleave a list with a write and cross their replies.
        """
        async with self._lock:
            await self._ensure()
            resolved = await resolve_id(self._link, kind, prefix)  # type: ignore[arg-type]
            payload = dict(data or {})
            payload["id"] = resolved
            return await self._request(op, payload)

    async def _request(self, op: str, data: dict | None) -> dict:
        """One request, replayed at most once and only when that is safe.

        A lost reply is indistinguishable from a write that was applied but
        whose answer never arrived, so a mutation is never replayed silently:
        either the caller made it replay-safe (an id it supplied itself) or the
        error surfaces and the caller has to look before acting.
        """
        try:
            response = await self._link.request(op, data)  # type: ignore[union-attr]
        except TRANSPORT_ERRORS + (GwbError,) as error:
            if isinstance(error, GwbError) and error.code not in RESYNC_CODES:
                raise
            await self._drop()
            if not is_replayable(op, data):
                raise GwbError(
                    "link_lost",
                    f"链路在 {op} 途中断了。这条写入可能生效了也可能没有："
                    f"先 list 核对，不要直接重发（{error}）",
                ) from error
            await self._ensure()
            response = await self._link.request(op, data)  # type: ignore[union-attr]
        self._last = time.monotonic()
        return check(response)

    async def close(self) -> None:
        async with self._lock:
            await self._drop()

    async def _ensure(self) -> None:
        if self._link is not None and self._link.connected:
            if time.monotonic() - self._last <= self.IDLE_CLOSE:
                return
            await self._drop()
        self._link = await connect()
        self._last = time.monotonic()

    async def _drop(self) -> None:
        if self._link is not None:
            await self._link.close()
            self._link = None
