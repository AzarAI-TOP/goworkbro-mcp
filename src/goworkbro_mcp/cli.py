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

        if args.cmd == "todo":
            await todo(link, args)
        elif args.cmd == "habit":
            await habit(link, args)
        elif args.cmd == "cd":
            await countdown(link, args)


async def todo(link: core.Link, args) -> None:
    if args.action == "add":
        data = {"title": args.title}
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
    elif args.action == "rm":
        todo_id = await core.resolve_id(link, "todos", args.id)
        if not args.yes:
            sys.exit(f"删除待办会真的删掉 {short(todo_id)}，确认就加 --yes")
        core.check(await link.request("todo.delete", {"id": todo_id}))
        print(f"已删除 {short(todo_id)}")


async def habit(link: core.Link, args) -> None:
    if args.action == "add":
        data = {"title": args.title}
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
    elif args.action == "rm":
        habit_id = await core.resolve_id(link, "habits", args.id)
        if not args.yes:
            sys.exit(f"删除习惯会真的删掉 {short(habit_id)}，确认就加 --yes")
        core.check(await link.request("habit.delete", {"id": habit_id}))
        print(f"已删除 {short(habit_id)}")


async def countdown(link: core.Link, args) -> None:
    if args.action == "add":
        data = {"title": args.title, "target_at": args.when}
        if args.color is not None:
            data["color_index"] = args.color
        created = core.check(await link.request("countdown.add", data))
        print(f"新建 {short(created['id'])}  {args.title} @ {args.when}")
    elif args.action == "rm":
        countdown_id = await core.resolve_id(link, "countdowns", args.id)
        if not args.yes:
            sys.exit(f"删除倒计时会真的删掉 {short(countdown_id)}，确认就加 --yes")
        core.check(await link.request("countdown.delete", {"id": countdown_id}))
        print(f"已删除 {short(countdown_id)}")


# ---- argparse --------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gwb", description="通过蓝牙读写手机上的 GoWorkBro")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON（给脚本/agent 用）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping", help="连通性 + 各表条数")
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
