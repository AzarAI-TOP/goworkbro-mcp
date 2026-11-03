# goworkbro-mcp

[GoWorkBro](https://github.com/AzarAI-TOP/GoWorkBro)（本地单机的 Android 生活自律 app）的
**电脑协同**客户端：一个 **MCP 服务器** + 一个 **命令行工具**，让电脑（以及 agent）通过
**蓝牙 LE** 读写手机上的待办 / 习惯 / 倒计时。

- **手机是唯一数据源**：每次调用就是一条真实写入，手机界面立刻刷新。没有双向同步、没有冲突合并。
- **不联网**：手机做 BLE 外设（只广播、不扫描、不申请定位权限），电脑做 central，直连。
- 手机端要求：GoWorkBro **≥ 3.7.0**，并在 `设置 → 电脑协同` 打开开关（默认关闭）。

## 安装

```bash
# 从 git 装（已发布的 wheel / sdist 在 Releases 里）
uv tool install git+https://github.com/AzarAI-TOP/goworkbro-mcp
# 或者离线装 release 里的 wheel
uv tool install https://github.com/AzarAI-TOP/goworkbro-mcp/releases/download/v0.1.0/goworkbro_mcp-0.1.0-py3-none-any.whl
# 或者用 pipx
pipx install goworkbro-mcp
```

装完得到两个命令：`gwb`（CLI）与 `gwb-mcp`（MCP 服务器，stdio 传输）。

依赖只有 [`bleak`](https://github.com/hbldh/bleak) 和官方 [`mcp`](https://github.com/modelcontextprotocol/python-sdk)。

## 配 token

手机打开 `设置 → 电脑协同`，复制那串 token，在本机存一次（只需一次）：

```bash
gwb token 3f9c0a7d4b6e…        # 写到 ~/.config/gwb/config.json，权限 0600
```

也可以用 `GWB_TOKEN` 环境变量覆盖，适合临时/多机场景。

## 命令行

```bash
gwb ping                        # 连通性 + 各表条数（首次约 3–6 秒，含蓝牙扫描）
gwb todos | habits | countdowns # 列表（--json 给脚本吃）

gwb todo add "写毕设" --icon book --color 2 --timing countdown --minutes 25
gwb todo done 5c32b591          # id 支持唯一前缀；--undone 改回未完成
gwb todo edit 5c32b591 --title "写毕设(改)"   # 只改传了的字段
gwb todo rm 5c32b591 -y         # 不给 -y 就拒绝

gwb habit add "读书" --target 2 --unit 章
gwb habit check 6f9ccd4d        # 默认 +1（已达标时 -1，与手机上点一下一致）
gwb habit check 6f9ccd4d --delta -1

gwb cd add "二课·香文化" 2026-11-05T14:00
gwb cd rm 938ab965 -y
```

`--icon` 可选值：`scroll book cup dumbbell dog cat star apple music flame moon sun pencil
flower sprout file heart bolt basketball coffee cloud cherry fish gamepad bulb gift clock`。

## 接进 agent（MCP）

`gwb-mcp` 是标准 stdio MCP 服务器，22 个工具：

| 工具 | 作用 |
|---|---|
| `ping` | 链路自检 + 各表条数 |
| `list_todos` / `list_habits` / `list_countdowns` | 列表（待办与手机待办页同序） |
| `add_todo` / `update_todo` / `set_todo_done` / `delete_todo` | 待办增/改字段/改完成/删 |
| `add_habit` / `update_habit` / `check_habit` / `delete_habit` | 习惯增/改/打卡/删 |
| `add_countdown` / `update_countdown` / `delete_countdown` | 倒计时增/改/删 |
| `today_summary` | 今天全景：待办/习惯/喝水/睡眠/专注/下一个倒计时（一帧） |
| `log_water` / `water_today` | 记一杯水 / 今日喝水明细 |
| `list_sleep` / `log_sleep` | 最近睡眠记录（默认 14 天窗口）/ 补一条 |
| `focus_today` | 今日专注轮次、分钟、按待办分组 |
| `disconnect` | 主动断开，省手机电（下次调用自动重连） |

`list_todos` 可以传 `only=open`（只看未完成）或 `only=today`（未完成 + 今天完成的），
完成历史会一直长，只找活的时候别拉全量。`add_*` 都接受可选 `id`：传了就是 upsert，
同一个 id 重试不会变两条。

`today_summary` / `log_water` / `water_today` / `list_sleep` / `log_sleep` / `focus_today`
需要手机端 **GoWorkBro 3.8+**（协议 v2）。旧版 app 上调用会得到一条
`too_old: … 先升级 app`，其余工具照常工作。

注册示例：

```bash
# pi
pi mcp add goworkbro -- ~/.local/bin/gwb-mcp

# Claude Code
claude mcp add goworkbro -- ~/.local/bin/gwb-mcp
```

```jsonc
// Cursor / 其他 ~/.config/*/mcp.json
{ "mcpServers": { "goworkbro": { "command": "~/.local/bin/gwb-mcp" } } }
```

```toml
# Codex ~/.codex/config.toml
[mcp_servers.goworkbro]
command = "~/.local/bin/gwb-mcp"
```

行为特性：

- **连接在调用之间复用**：一次扫描约 3 秒，若每条命令都重扫就没法用了；实测首次约 3–6 秒，
  之后单次约 0.1–0.2 秒（2026-11-03 实测：协商 MTU 前 `ping` 0.34 s / `todos.list` 0.5 s）。空闲 5 分钟自动断开（BLE 连接态耗手机电）。
- **链路掉了重连一次**，但不重发已经发出的写入 —— 宁可报错，也不在你的库里留重复条目。
- **删除类工具必须 `confirm=true`**，否则以工具错误返回（模型能看见原因），不会静默删。
- **并发调用安全**：所有走蓝牙的操作（含 id 解析）在同一把锁里串行，回复按回显的 `op` 校验，失步就丢弃连接重连。
- **写操作的重放**：`add_*` 由客户端生成 id，重试不会建出第二条；其余写在链路中断时只报 `link_lost`，不自动重发（丢响应与写已生效无法区分），先 list 核对。

## 安全

- 手机端默认关闭；打开时生成 token，**每一条请求都校验**。BLE 配对是 Just Works（不做认证），
  所以真正管权限的是 token。
- token 存在 app 的私有设置表里，因此会随它的 JSON 备份一起导出 —— 备份文件等于凭据。
- 换手机 / 怀疑泄漏时，在设置里关掉再打开「电脑协同」即可作废旧 token。

## 已知环境问题

- Linux 需要 BlueZ 跑着（`bluetoothctl show` 能看到控制器、`Powered: yes`）。
- 手机的 BLE 地址会轮换，所以**不缓存 MAC**，每次扫描服务 UUID —— 不要「优化」掉这一步。
- 手机侧的 BLE 广播靠前台服务维持；MIUI/澎湃建议给 app 设「无限制」电池策略。

## 协议

线上协议见 [docs/protocol.md](docs/protocol.md)（与 app 仓库的 `docs/remote-protocol.md` 同源）。

## License

MIT
