"""Transport + protocol glue for GoWorkBro 电脑协同 (BLE), shared by the CLI and the MCP server.

手机是唯一数据源：这里只发命令、收结果，没有双向同步。
协议见 docs/remote-protocol.md。
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import time
import warnings
from pathlib import Path

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
    """One request at a time; each waits for its reply frame on DOWN."""

    def __init__(self, client: BleakClient, token: str) -> None:
        self.client = client
        self.token = token
        self.reader = Reassembler()
        self.pending: asyncio.Future | None = None
        # Linux/BlueZ does not report the negotiated MTU; the ATT floor is safe.
        self.chunk = min(MAX_ATTRIBUTE, max(20, (getattr(client, "mtu_size", 23) or 23) - 3))

    async def open(self) -> None:
        await self.client.start_notify(DOWN, self._on_notify)
        info = json.loads((await self.client.read_gatt_char(INFO)).decode("utf-8"))
        if info.get("app") != "GoWorkBro":
            raise GwbError("not_goworkbro", f"对面不是 GoWorkBro：{info}")

    def _on_notify(self, _char, data: bytearray) -> None:
        for text in self.reader.feed(bytes(data)):
            if self.pending is not None and not self.pending.done():
                self.pending.set_result(json.loads(text))

    async def request(self, op: str, data: dict | None = None, timeout: float = 10.0) -> dict:
        payload = {"v": 1, "token": self.token, "op": op}
        if data:
            payload["data"] = data
        self.pending = asyncio.get_running_loop().create_future()
        body = frame(payload)
        for start in range(0, len(body), self.chunk):
            await self.client.write_gatt_char(UP, body[start:start + self.chunk], response=True)
        return await asyncio.wait_for(self.pending, timeout)

    async def close(self) -> None:
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


async def connect(timeout: float = 12.0) -> Link:
    device = await find_phone(timeout)
    client = BleakClient(device, timeout=25)
    await client.connect()
    if not client.is_connected:
        raise GwbError("not_connected", "连上了但没建立会话，再试一次？")
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


async def resolve_id(link: Link, kind: str, prefix: str) -> str:
    """A full id, or the unique prefix of one (ids are UUIDs — nobody types them)."""
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
    [IDLE_CLOSE] seconds of silence (BLE connected state costs phone battery)
    and transparently re-established — once — when the radio loses the peer.
    """

    IDLE_CLOSE = 300.0

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._link: Link | None = None
        self._last = 0.0

    async def link(self) -> Link:
        async with self._lock:
            await self._ensure()
            return self._link  # type: ignore[return-value]

    async def call(self, op: str, data: dict | None = None) -> dict:
        async with self._lock:
            await self._ensure()
            try:
                response = await self._link.request(op, data)  # type: ignore[union-attr]
            except (asyncio.TimeoutError, OSError, EOFError):
                # The phone went out of range or the service restarted: one retry
                # on a fresh link, never a silent second write of a mutation.
                await self._drop()
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
