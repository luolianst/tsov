# tsov-host IPC v0（骨架契约）

状态：**v0 子集已实现（S2.1，2026-10-18）**｜传输：本机回环 TCP，换行分隔 JSON（NDJSON）

已实现：`hello`、`transport`（play/stop/seek）、`state`、`shutdown`；Python 客户端 `tsov/host/host_client.py` 支持启动/握手/状态/传输/崩溃重启。
待实现：`load_graph`、`patch`（E1 命令级增量 + revision 断档）；当前 `revision` 固定 0。

## 目标

Python（`tsov/host/host_client.py`）与 sidecar（`tsov-host.exe`）之间的最小控制面。音频线程不读 socket；网络线程解析后投递无锁队列。

## 握手

客户端连接后必须首先发送：

```json
{"jsonrpc":"2.0","id":1,"method":"hello","params":{"protocol":0,"client":"tsov-python"}}
```

服务端回：

```json
{"jsonrpc":"2.0","id":1,"result":{"protocol":0,"host":"tsov-host","version":"0.1.0","sampleRate":48000,"blockSize":128}}
```

协议版本不匹配 → 服务端回 `error.code=-32000` 并关闭连接。

## 方法（v0）

| 方法 | 参数 | 返回 | 说明 |
|---|---|---|---|
| `hello` | `protocol`, `client` | 版本/设备信息 | 必须首先调用 |
| `load_graph` | `graph`（工程快照） | `revision` | 全量装载；S1 实现 |
| `patch` | `revision`, `ops[]` | `revision` | E1 命令级增量；revision 断档回 `-32001` 要求全量 |
| `transport` | `action`（play/stop/seek/loop） | `ok` | 实时传输控制；已实现 play/stop/seek（seek 用 `position` 样本） |
| `state` | — | `playhead`, `xruns`, `tracks[].peak`, `revision` | 已实现；30Hz 推送待做 |
| `shutdown` | `graceful` | `ok` | 停引擎并退出 |

## 通知（服务端 → 客户端）

```json
{"jsonrpc":"2.0","method":"state","params":{"playhead":12.345,"xruns":0,"cpu":0.12,"revision":42}}
{"jsonrpc":"2.0","method":"error","params":{"code":-32002,"message":"plugin crashed","track":3}}
```

## 约束

- 只绑定 `127.0.0.1`；v0 不做鉴权（本机单用户），S1 起加临时 token。
- `revision` 单调递增；`patch` 幂等，重复 revision 直接返回当前 revision。
- 音频线程只消费已排序事件队列；不得在回调内做 JSON/分配/锁。
- 崩溃/重启后客户端重连 → `hello` → 全量恢复；当前由 `HostClient.restart()` 重新拉起同一 manifest 并重载每轨 VST3 state。
