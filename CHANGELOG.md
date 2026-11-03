# Changelog

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循
[SemVer](https://semver.org/lang/zh-CN/)。

## [0.1.1] - 2026-11-03

真机回归暴露的两个传输层缺陷 + 一轮延迟优化。

### Fixed

- **并发调用串台**：`_resolve` 在锁外发请求，两个并发的工具调用会共用同一个
  待回复槽位，回复互相错位（实测把待办列表当成倒计时列表返回，并用串来的 id 标错了
  待办）。现在 id 解析与写入在同一把锁内完成（`Phone.call_id`）。
- **回复不再靠运气**：`Link.request` 校验回复回显的 `op`，对不上即判 `desync`，
  丢弃连接重连；超时/异常后清空待回复槽位，迟到的帧不会被下一条请求认领。
- **重试不再重复写**：只有只读 op 和客户端自带 id 的 `*.add` 会被重放；其余
  （`habit.check`、`*.update`、`*.delete`、`todo.done`）中断时报 `link_lost`，
  交给调用方核对——丢响应和写已生效在 BLE 上无法区分。
- CLI/MCP 的 `add_*` 现在自己生成 id，重放幂等。
- 删除类：先检查确认参数再解析 id（省一个往返，手机不在旁边时提示也不会被蓝牙错误盖住）。

### Changed

- **MTU 协商**：连上后调 bleak 的 `_acquire_mtu()`，分片从 20 字节提到 244 字节
  （手机侧 `onMtuChanged` 自适应）。
- 全量 UUID 直接当成 id 用，省掉一次 `list` 往返（`set_todo_done` / `delete_*` 立省 ~0.4 s）。
- 单次请求超时 10 s → 6 s。
- MCP server 的 `version` 用包版本（原来是字面量 `"1"`）；`check_habit` 文案修笔误。

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
