#!/usr/bin/env python3
"""GoWorkBro 电脑协同 CLI —— 通过蓝牙读写手机上的待办/习惯/倒计时。

手机是唯一数据源：每个子命令都是一条命令式写入，没有双向同步、没有冲突合并。
协议与字段见 GoWorkBro 仓库的 docs/remote-protocol.md；传输实现在 core.py（CLI 与 MCP 服务器共用）。

依赖只有 bleak：
    uv run --with bleak gwb ping
    或  pipx install goworkbro-mcp  (或 uv tool install .)

配置（token 只需存一次）：
    gwb token <设置 → 电脑协同 里的那串>    # 写到 ~/.config/gwb/config.json
    也可用 $GWB_TOKEN 覆盖。手机端要先打开 设置 → 电脑协同。
"""

import argparse
import asyncio
import json
import sys

from . import core


# ---- rendering -------------------------------------------------------------


def short(item_id: str) -> str:
    return item_id[:8]


def print_todos(items: list) -> None:
    for item in items:
        mark = "[x]" if item["is_done"] else "[ ]"
        when = f" 完成于 {item['done_date']}" if item["done_date"] else ""
        print(f"{mark} {short(item['id'])}  {item['title']}  ({item['icon']}){when}")


def print_habits(items: list) -> None:
    for item in items:
        mark = "[x]" if item.get("is_completed") else "[ ]"
        print(f"{mark} {short(item['id'])}  {item['title']}"
              f"  {item['current_count']}/{item['target_count']} {item['unit']}")


def print_countdowns(items: list) -> None:
    for item in items:
        print(f"{short(item['id'])}  {item['target_at']}  {item['title']}")


def print_summary(data: dict) -> None:
    todos = data.get("todos", {})
    habits = data.get("habits", {})
    water = data.get("water", {})
    sleep = data.get("sleep", {})
    focus = data.get("focus", {})
    last = sleep.get("last")
    print(f"今天 {data.get('today')}")
    print(f"  待办   未完成 {todos.get('undone')} · 今日完成 {todos.get('done_today')} / 共 {todos.get('total')}")
    print(f"  习惯   达标 {habits.get('done')} / {habits.get('total')}")
    print(f"  喝水   {water.get('ml')} / {water.get('goal_ml')} ml")
    night = f"{sleep.get('today_minutes')} min" if sleep.get("today_minutes") else "还没记"
    streak = sleep.get("streak") or 0
    print(f"  睡眠   {night} · 连续达标 {streak} 天")
    if last:
        print(f"         最近一次 {last.get('sleep_at')} → {last.get('wake_at')}（{last.get('minutes')} min）")
    print(f"  专注   {focus.get('sessions')} 轮 / {focus.get('minutes')} min")
    nxt = data.get("next_countdown")
    if nxt:
        print(f"  倒计时 {nxt.get('target_at')}  {nxt.get('title')}")


def print_water(data: dict) -> None:
    print(f"{data.get('date')}  {data.get('ml')} / {data.get('goal_ml')} ml")
    for log in data.get("logs", []):
        print(f"  {log.get('logged_at')}  {log.get('ml')} ml  {short(log.get('id', ''))}")


def print_sleep(data: dict) -> None:
    print(f"今天 {data.get('today_minutes')} min · 窗口起点 {data.get('from')}")
    for item in data.get("items", []):
        print(f"  {short(item['id'])}  {item['sleep_at']} → {item['wake_at']}"
              f"  {item['minutes']} min  ({item['day']})")


def print_focus(data: dict) -> None:
    print(f"{data.get('date')}  {data.get('sessions')} 轮 / {data.get('minutes')} min")
    for row in data.get("by_todo", []):
        title = row.get("title") or short(row.get("todo_id", ""))
        print(f"  {row.get('minutes')} min  {title}")


PRINTERS = {"todos": print_todos, "habits": print_habits, "countdowns": print_countdowns}


# ---- commands --------------------------------------------------------------


async def run(args) -> None:
    async with core.Session() as link:
        if args.cmd == "ping":
            data = core.check(await link.request("ping"))
            print(json.dumps(data, ensure_ascii=False) if args.json else
                  f"protocol {data['protocol']} · 今天 {data['today']} · "
                  f"待办 {data['todos']} · 习惯 {data['habits']} · 倒计时 {data['countdowns']}")
            return

        if args.cmd in core.LIST_OPS:
            items = await core.list_items(link, args.cmd)
            if args.json:
                print(json.dumps(items, ensure_ascii=False, indent=1))
            elif not items:
                print("（空）")
            else:
                PRINTERS[args.cmd](items)
            return

        if args.cmd == "today":
            data = core.check(await link.request("today.summary"))
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=1))
            else:
                print_summary(data)
            return

        if args.cmd == "water":
            data = core.check(await link.request("water.log", {"id": core.new_id(), "ml": args.ml}))
            print(json.dumps(data, ensure_ascii=False) if args.json else
                  f"记下 {data['ml']} ml · 今天 {data['today_ml']} / {data['goal_ml']} ml")
            return

        if args.cmd == "sleep":
            if args.action == "log":
                data = core.check(await link.request("sleep.log", {
                    "id": core.new_id(), "sleep_at": args.start, "wake_at": args.end,
                }))
                print(json.dumps(data, ensure_ascii=False) if args.json else
                      f"记下 {data['minutes']} min · 归到 {data['day']}")
            else:
                data = core.check(await link.request("sleep.list", {"days": args.days}))
                if args.json:
                    print(json.dumps(data, ensure_ascii=False, indent=1))
                else:
                    print_sleep(data)
            return

        if args.cmd == "focus":
            data = core.check(await link.request("focus.today"))
            if args.json:
                print(json.dumps(data, ensure_ascii=False, indent=1))
            else:
                print_focus(data)
            return

        if args.cmd == "todo":
            await todo(link, args)
        elif args.cmd == "habit":
            await habit(link, args)
        elif args.cmd == "cd":
            await countdown(link, args)


async def todo(link: core.Link, args) -> None:
    if args.action == "add":
        data = {"id": core.new_id(), "title": args.title}
        for key, field in (("icon", "icon"), ("color", "color_index"),
                           ("timing", "timing_type"), ("minutes", "duration_minutes")):
            value = getattr(args, key)
            if value is not None:
                data[field] = value
        created = core.check(await link.request("todo.add", data))
        print(f"新建 {short(created['id'])}  {args.title}")
    elif args.action == "done":
        todo_id = await core.resolve_id(link, "todos", args.id)
        data = core.check(
            await link.request("todo.done", {"id": todo_id, "done": not args.undone}),
        )
        print(f"{short(data['id'])} → {('已完成 ' + data['done_date']) if data['is_done'] else '未完成'}")
    elif args.action == "edit":
        data = {}
        for key, field in (("title", "title"), ("icon", "icon"), ("color", "color_index"),
                           ("timing", "timing_type"), ("minutes", "duration_minutes")):
            value = getattr(args, key)
            if value is not None:
                data[field] = value
        if not data:
            sys.exit("没给要改的字段：--title / --icon / --color / --timing / --minutes")
        todo_id = await core.resolve_id(link, "todos", args.id)
        core.check(await link.request("todo.update", {"id": todo_id, **data}))
        print(f"已修改 {short(todo_id)}")
    elif args.action == "rm":
        if not args.yes:
            sys.exit(f"删除待办不可恢复：确认就加 --yes（id {args.id}）")
        todo_id = await core.resolve_id(link, "todos", args.id)
        core.check(await link.request("todo.delete", {"id": todo_id}))
        print(f"已删除 {short(todo_id)}")


async def habit(link: core.Link, args) -> None:
    if args.action == "add":
        data = {"id": core.new_id(), "title": args.title}
        for key, field in (("icon", "icon"), ("color", "color_index"),
                           ("target", "target_count"), ("unit", "unit")):
            value = getattr(args, key)
            if value is not None:
                data[field] = value
        created = core.check(await link.request("habit.add", data))
        print(f"新建 {short(created['id'])}  {args.title}")
    elif args.action == "check":
        habit_id = await core.resolve_id(link, "habits", args.id)
        data = {"id": habit_id}
        if args.delta is not None:
            data["delta"] = args.delta
        result = core.check(await link.request("habit.check", data))
        state = "已达标" if result["is_completed"] else "未达标"
        print(f"{short(result['id'])}  {result['current_count']}/{result['target_count']}  {state}")
    elif args.action == "edit":
        data = {}
        for key, field in (("title", "title"), ("icon", "icon"), ("color", "color_index"),
                           ("target", "target_count"), ("unit", "unit")):
            value = getattr(args, key)
            if value is not None:
                data[field] = value
        if not data:
            sys.exit("没给要改的字段：--title / --icon / --color / --target / --unit")
        habit_id = await core.resolve_id(link, "habits", args.id)
        core.check(await link.request("habit.update", {"id": habit_id, **data}))
        print(f"已修改 {short(habit_id)}")
    elif args.action == "rm":
        if not args.yes:
            sys.exit(f"删除习惯不可恢复：确认就加 --yes（id {args.id}）")
        habit_id = await core.resolve_id(link, "habits", args.id)
        core.check(await link.request("habit.delete", {"id": habit_id}))
        print(f"已删除 {short(habit_id)}")


async def countdown(link: core.Link, args) -> None:
    if args.action == "add":
        data = {"id": core.new_id(), "title": args.title, "target_at": args.when}
        if args.color is not None:
            data["color_index"] = args.color
        created = core.check(await link.request("countdown.add", data))
        print(f"新建 {short(created['id'])}  {args.title} @ {args.when}")
    elif args.action == "edit":
        data = {}
        if args.title is not None:
            data["title"] = args.title
        if args.when is not None:
            data["target_at"] = args.when
        if args.color is not None:
            data["color_index"] = args.color
        if not data:
            sys.exit("没给要改的字段：--title / --when / --color")
        countdown_id = await core.resolve_id(link, "countdowns", args.id)
        core.check(await link.request("countdown.update", {"id": countdown_id, **data}))
        print(f"已修改 {short(countdown_id)}")
    elif args.action == "rm":
        if not args.yes:
            sys.exit(f"删除倒计时不可恢复：确认就加 --yes（id {args.id}）")
        countdown_id = await core.resolve_id(link, "countdowns", args.id)
        core.check(await link.request("countdown.delete", {"id": countdown_id}))
        print(f"已删除 {short(countdown_id)}")


# ---- argparse --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gwb", description="通过蓝牙读写手机上的 GoWorkBro")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON（给脚本/agent 用）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping", help="连通性 + 各表条数")
    sub.add_parser("today", help="今天的一帧全景（待办/习惯/喝水/睡眠/专注/下一个倒计时）")
    water = sub.add_parser("water", help="记一杯水（默认第一档快捷杯量）")
    water.add_argument("--ml", type=int)
    sleep = sub.add_parser("sleep", help="睡眠记录：默认列出，log 补一条")
    sleep.add_argument("--days", type=int, default=14)
    sleep_sub = sleep.add_subparsers(dest="action")
    slog = sleep_sub.add_parser("log", help="补一条：本地时间，如 2026-11-04T23:00")
    slog.add_argument("start")
    slog.add_argument("end")
    sub.add_parser("focus", help="今天的专注统计")
    for name in core.LIST_OPS:
        sub.add_parser(name, help=f"列出 {name}")

    todo = sub.add_parser("todo", help="待办增改删")
    todo_sub = todo.add_subparsers(dest="action", required=True)
    add = todo_sub.add_parser("add", help="新建")
    add.add_argument("title")
    add.add_argument("--icon")
    add.add_argument("--color", type=int)
    add.add_argument("--timing", choices=["none", "forward", "countdown"])
    add.add_argument("--minutes", type=int)
    edit = todo_sub.add_parser("edit", help="改标题/图标/颜色/计时（不传的字段不动）")
    edit.add_argument("id")
    edit.add_argument("--title")
    edit.add_argument("--icon")
    edit.add_argument("--color", type=int)
    edit.add_argument("--timing", choices=["none", "forward", "countdown"])
    edit.add_argument("--minutes", type=int)
    done = todo_sub.add_parser("done", help="标记完成 / --undone 改回未完成")
    done.add_argument("id", help="id 或它的前缀")
    done.add_argument("--undone", action="store_true")
    rm = todo_sub.add_parser("rm", help="删除（要 --yes）")
    rm.add_argument("id")
    rm.add_argument("-y", "--yes", action="store_true")

    habit = sub.add_parser("habit", help="习惯增改删")
    habit_sub = habit.add_subparsers(dest="action", required=True)
    hadd = habit_sub.add_parser("add", help="新建")
    hadd.add_argument("title")
    hadd.add_argument("--target", type=int)
    hadd.add_argument("--unit")
    hadd.add_argument("--icon")
    hadd.add_argument("--color", type=int)
    hedit = habit_sub.add_parser("edit", help="改标题/目标/单位/图标/颜色")
    hedit.add_argument("id")
    hedit.add_argument("--title")
    hedit.add_argument("--target", type=int)
    hedit.add_argument("--unit")
    hedit.add_argument("--icon")
    hedit.add_argument("--color", type=int)
    check_cmd = habit_sub.add_parser("check", help="打卡；--delta -1 撤销一下")
    check_cmd.add_argument("id")
    check_cmd.add_argument("--delta", type=int)
    hrm = habit_sub.add_parser("rm", help="删除（要 --yes）")
    hrm.add_argument("id")
    hrm.add_argument("-y", "--yes", action="store_true")

    cd = sub.add_parser("cd", help="倒计时增删")
    cd_sub = cd.add_subparsers(dest="action", required=True)
    cadd = cd_sub.add_parser("add", help="新建，when 用本地时间 2026-11-20T09:00")
    cadd.add_argument("title")
    cadd.add_argument("when")
    cadd.add_argument("--color", type=int)
    cedit = cd_sub.add_parser("edit", help="改标题/到期时间/颜色")
    cedit.add_argument("id")
    cedit.add_argument("--title")
    cedit.add_argument("--when")
    cedit.add_argument("--color", type=int)
    crm = cd_sub.add_parser("rm", help="删除（要 --yes）")
    crm.add_argument("id")
    crm.add_argument("-y", "--yes", action="store_true")

    token = sub.add_parser("token", help="保存 设置 → 电脑协同 里的 token")
    token.add_argument("value")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.cmd == "token":
        print(f"已保存到 {core.save_token(args.value)}")
        return
    try:
        asyncio.run(run(args))
    except core.GwbError as error:
        sys.exit(str(error))
    except asyncio.TimeoutError:
        sys.exit("手机没回话：协同开着吗？蓝牙够近吗？")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
