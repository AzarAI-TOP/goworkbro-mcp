#!/usr/bin/env python3
"""GoWorkBro 电脑协同 MCP server（stdio）。

把手机上的待办/习惯/倒计时的读写能力暴露成 MCP 工具，供 agent 直接调用。
传输走蓝牙（BLE GATT），不经过任何网络；协议见 docs/remote-protocol.md。

运行/注册：
    pi mcp add goworkbro -- ~/.local/bin/gwb-mcp

手机端要先在 设置 → 电脑协同 打开开关，PC 上先存一次 token（gwb token <token>）。
"""

import asyncio

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import core
from . import __version__

INSTRUCTIONS = """读写手机上 GoWorkBro 的待办 / 习惯 / 倒计时 / 喝水 / 睡眠，走蓝牙，不联网。

- 手机是唯一数据源：每次调用就是一条真实写入，立刻生效，Room 会让手机界面自动刷新。
- 没有双向同步、没有冲突合并、没有撤销栈。删了就是删了（删除类工具必须显式 confirm=true）。
- 写之前先 list 一次拿 id；后续操作用 id，也接受唯一前缀（如 "5c32b591"）。给完整 UUID 更快（跳过一次扫描）。
- 新建工具每次都会带上自己生成的 id，所以重试不会建出两条。
- 改东西用 update_*（只改传了的字段），不要「删了重建」——重建会丢掉完成记录和计数。
- 想回答「今天怎么样」先调 today_summary（一帧到位），别拉四张表。
- 日期时间一律本地时间、无时区：add_countdown 的 when 形如 2026-11-20T09:00，
  log_sleep 的 sleep_at / wake_at 形如 2026-11-04T23:00。
- today_summary / log_water / water_today / list_sleep / log_sleep / focus_today
  需要手机端 GoWorkBro 3.8+（协议 v2）；旧版会报 too_old 并说明怎么升。
- 手机端未打开「电脑协同」、token 不对、手机不在附近时，工具会报错并说明怎么修，不要盲目重试。
- 并发调用是安全的（服务器内部串行化）；但不要用两次调用去抢同一条数据。
"""

server = MCPServer(name="goworkbro", instructions=INSTRUCTIONS, version=__version__)

_phone = core.Phone()


async def _call(op: str, data: dict | None = None) -> dict:
    try:
        return await _phone.call(op, data)
    except core.GwbError as error:
        raise ToolError(f"{error.code}: {error.message}") from error
    except asyncio.TimeoutError as error:
        raise ToolError("手机没回话：协同开着吗？蓝牙够近吗？") from error


# Protocol v2 ops (water / sleep / focus / summary). Asking first turns a
# confusing "bad_op" into "upgrade the app", which is what the user must do.
V2 = 2


async def _call_v2(op: str, data: dict | None = None) -> dict:
    try:
        await _phone.require(V2)
    except core.GwbError as error:
        raise ToolError(f"{error.code}: {error.message}") from error
    except asyncio.TimeoutError as error:
        raise ToolError("手机没回话：协同开着吗？蓝牙够近吗？") from error
    return await _call(op, data)


async def _call_id(kind: str, op: str, prefix: str, data: dict | None = None) -> dict:
    """Resolve the id and run the op in one locked turn (no half-done pair)."""
    try:
        return await _phone.call_id(op, kind, prefix, data)
    except core.GwbError as error:
        raise ToolError(f"{error.code}: {error.message}") from error
    except asyncio.TimeoutError as error:
        raise ToolError("手机没回话：协同开着吗？蓝牙够近吗？") from error




@server.tool()
async def ping() -> dict:
    """检查链路是否活着，并返回各表条数与手机端协议版本。第一次调用会扫蓝牙（约 3 秒）。"""
    return await _call("ping")


@server.tool()
async def list_todos(only: str | None = None) -> dict:
    """列出待办，顺序与手机待办页一致：未完成在前、新的在上。

    only: all（默认，含全部完成历史）/ open（只看未完成）/ today（未完成 + 今天完成的，
    就是手机待办页上肉眼可见的那些）。历史会一直长，只想找活的时候用 open。
    """
    # `only` itself is a v2 field: a v1 phone would ignore it and answer with
    # everything, which looks like a successful filter that never happened.
    if only is None:
        return {"items": (await _call("todos.list")).get("items", [])}
    return {"items": (await _call_v2("todos.list", {"only": only})).get("items", [])}


@server.tool()
async def list_habits() -> dict:
    """列出全部习惯，含当前计数 current_count / 目标 target_count 与今日是否达标。"""
    return {"items": (await _call("habits.list")).get("items", [])}


@server.tool()
async def list_countdowns() -> dict:
    """列出全部倒计时，按到期时间升序；target_at 是本地时间字符串。"""
    return {"items": (await _call("countdowns.list")).get("items", [])}


@server.tool()
async def add_todo(
    title: str,
    icon: str | None = None,
    color: int | None = None,
    timing: str | None = None,
    minutes: int | None = None,
    id: str | None = None,
) -> dict:
    """新建一条待办，返回它的 id。

    icon 取 CartoonIcons 的 key（scroll book cup dumbbell dog cat star apple music flame
    moon sun pencil flower sprout file heart bolt basketball coffee cloud cherry fish
    gamepad bulb gift clock），默认 scroll。
    color 是图标底色下标 0..7。timing 为 none / forward / countdown，countdown 时用
    minutes 给时长（分钟）。
    """
    data = {"id": id or core.new_id(), "title": title}
    if icon is not None:
        data["icon"] = icon
    if color is not None:
        data["color_index"] = color
    if timing is not None:
        data["timing_type"] = timing
    if minutes is not None:
        data["duration_minutes"] = minutes
    return await _call("todo.add", data)


@server.tool()
async def update_todo(
    id: str,
    title: str | None = None,
    icon: str | None = None,
    color: int | None = None,
    timing: str | None = None,
    minutes: int | None = None,
) -> dict:
    """改一条待办的标题/图标/颜色/计时模式；没传的字段保持原样，完成状态不受影响。

    只想改标题就别用 add_todo —— 那会带上一个新 id 造出第二条。
    """
    data = {}
    if title is not None:
        data["title"] = title
    if icon is not None:
        data["icon"] = icon
    if color is not None:
        data["color_index"] = color
    if timing is not None:
        data["timing_type"] = timing
    if minutes is not None:
        data["duration_minutes"] = minutes
    if not data:
        raise ToolError("没给要改的字段：title / icon / color / timing / minutes 至少给一个")
    return await _call_id("todos", "todo.update", id, data)


@server.tool()
async def set_todo_done(id: str, done: bool = True) -> dict:
    """把待办标记完成（done=true）或改回未完成（done=false）。完成日按手机当前日历日记。"""
    return await _call_id("todos", "todo.done", id, {"done": done})


@server.tool()
async def delete_todo(id: str, confirm: bool = False) -> dict:
    """删除一条待办。必须传 confirm=true —— 不可恢复，删除前先和用户确认。"""
    if not confirm:
        raise ToolError(f"删除待办是不可恢复的：先和用户确认，再传 confirm=true（id {id}）")
    return await _call_id("todos", "todo.delete", id)


@server.tool()
async def add_habit(
    title: str,
    target_count: int | None = None,
    unit: str | None = None,
    icon: str | None = None,
    color: int | None = None,
    id: str | None = None,
) -> dict:
    """新建一个习惯，返回它的 id。target_count 为每日目标次数（1..99，默认 1）。"""
    data = {"id": id or core.new_id(), "title": title}
    if target_count is not None:
        data["target_count"] = target_count
    if unit is not None:
        data["unit"] = unit
    if icon is not None:
        data["icon"] = icon
    if color is not None:
        data["color_index"] = color
    return await _call("habit.add", data)


@server.tool()
async def update_habit(
    id: str,
    title: str | None = None,
    target_count: int | None = None,
    unit: str | None = None,
    icon: str | None = None,
    color: int | None = None,
) -> dict:
    """改一个习惯的标题/目标次数/单位/图标/颜色；没传的字段保持原样，今日计数不受影响。"""
    data = {}
    if title is not None:
        data["title"] = title
    if target_count is not None:
        data["target_count"] = target_count
    if unit is not None:
        data["unit"] = unit
    if icon is not None:
        data["icon"] = icon
    if color is not None:
        data["color_index"] = color
    if not data:
        raise ToolError("没给要改的字段：title / target_count / unit / icon / color 至少给一个")
    return await _call_id("habits", "habit.update", id, data)


@server.tool()
async def check_habit(id: str, delta: int | None = None) -> dict:
    """打卡一次：默认 +1（已达标时 −1，与手机上点一下的行为一致），delta 可显式指定增减。"""
    data = {}
    if delta is not None:
        data["delta"] = delta
    return await _call_id("habits", "habit.check", id, data or None)


@server.tool()
async def delete_habit(id: str, confirm: bool = False) -> dict:
    """删除一个习惯。必须传 confirm=true —— 不可恢复，删除前先和用户确认。"""
    if not confirm:
        raise ToolError(f"删除习惯是不可恢复的：先和用户确认，再传 confirm=true（id {id}）")
    return await _call_id("habits", "habit.delete", id)


@server.tool()
async def add_countdown(
    title: str,
    when: str,
    color: int | None = None,
    id: str | None = None,
) -> dict:
    """新建倒计时。when 是本地时间、无时区，形如 2026-11-20T09:00 或 2026-11-20T09:00:00。"""
    data = {"id": id or core.new_id(), "title": title, "target_at": when}
    if color is not None:
        data["color_index"] = color
    return await _call("countdown.add", data)


@server.tool()
async def update_countdown(
    id: str,
    title: str | None = None,
    when: str | None = None,
    color: int | None = None,
) -> dict:
    """改一个倒计时的标题/到期时间/颜色；没传的字段保持原样。"""
    data = {}
    if title is not None:
        data["title"] = title
    if when is not None:
        data["target_at"] = when
    if color is not None:
        data["color_index"] = color
    if not data:
        raise ToolError("没给要改的字段：title / when / color 至少给一个")
    return await _call_id("countdowns", "countdown.update", id, data)


@server.tool()
async def delete_countdown(id: str, confirm: bool = False) -> dict:
    """删除一个倒计时。必须传 confirm=true —— 不可恢复，删除前先和用户确认。"""
    if not confirm:
        raise ToolError(f"删除倒计时是不可恢复的：先和用户确认，再传 confirm=true（id {id}）")
    return await _call_id("countdowns", "countdown.delete", id)


@server.tool()
async def today_summary() -> dict:
    """今天的一帧全景（需要 app 3.8+）：待办未完成/今日完成、习惯达标数、喝水 ml/目标、
    昨夜睡眠分钟 + 连续达标天数 + 最近一次记录、今日专注轮次/分钟、最近的倒计时。

    回答「今天怎么样」只需这一次调用，不要拿四张表拼。
    """
    return await _call_v2("today.summary")


@server.tool()
async def log_water(ml: int | None = None, id: str | None = None) -> dict:
    """记一杯水（需要 app 3.8+）。ml 省略时用手机上第一个快捷杯量（默认 150）。
    返回今日累计 today_ml 与目标 goal_ml。"""
    data = {"id": id or core.new_id()}
    if ml is not None:
        data["ml"] = ml
    return await _call_v2("water.log", data)


@server.tool()
async def water_today() -> dict:
    """今天的喝水明细（需要 app 3.8+）：总量、目标、快捷杯量、每一条记录。"""
    return await _call_v2("water.today")


@server.tool()
async def list_sleep(days: int | None = None) -> dict:
    """最近的睡眠记录（需要 app 3.8+），按醒来时间倒序，默认 14 天窗口。

    minutes 是单次时长；day 是这条被归到的日历日（醒来的那天），今天的总量看
    today_minutes。
    """
    data = {"days": days} if days is not None else None
    return await _call_v2("sleep.list", data)


@server.tool()
async def log_sleep(sleep_at: str, wake_at: str, id: str | None = None) -> dict:
    """补一条睡眠记录（需要 app 3.8+）：两个端点都是本地时间、无时区，
    形如 2026-11-04T23:00。wake_at 必须晚于 sleep_at 且不能是未来。"""
    return await _call_v2(
        "sleep.log", {"id": id or core.new_id(), "sleep_at": sleep_at, "wake_at": wake_at},
    )


@server.tool()
async def focus_today() -> dict:
    """今天的专注统计（需要 app 3.8+）：轮次、总分钟，以及按待办分组的分钟数。"""
    return await _call_v2("focus.today")


@server.tool()
async def disconnect() -> dict:
    """主动断开蓝牙连接（省手机电）。下次工具调用会自动重连。"""
    await _phone.close()
    return {"ok": True, "message": "已断开；下次调用会自动重连"}


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
