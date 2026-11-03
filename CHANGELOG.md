# Changelog

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循
[SemVer](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-11-03

从 GoWorkBro 仓库的 `tools/` 独立出来，成为可安装、可复用的客户端。

### Added

- **MCP 服务器** `gwb-mcp`（stdio）：13 个工具 —— `ping`、`list_todos|habits|countdowns`、
  `add_todo`、`set_todo_done`、`delete_todo`、`add_habit`、`check_habit`、`delete_habit`、
  `add_countdown`、`delete_countdown`、`disconnect`。
- **命令行** `gwb`：`ping` / `todos|habits|countdowns` / `todo add|done|rm` /
  `habit add|check|rm` / `cd add|rm` / `token`，`--json` 便于脚本。
- 连接在工具调用之间复用，空闲 5 分钟自动断开；链路中断只重连一次，不重发已发出的写入。
- 删除类操作需要显式确认（CLI `--yes`，MCP `confirm=true`）。

### 说明

- 手机端需要 GoWorkBro ≥ 3.7.0 且打开 `设置 → 电脑协同`。
- 协议与 app 的 `RemoteApi.PROTOCOL = 1` 对应，见 `docs/protocol.md`。
