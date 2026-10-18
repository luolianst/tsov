# tsov-host IPC v0（骨架契约）

状态：**v0 子集已实现（S2.1–S2.4，2026-10-18）**｜传输：本机回环 TCP，换行分隔 JSON（NDJSON）

已实现：`hello`、`load_graph`（全量活图）、`patch`（`set_notes` / `set_track_mix`）、`transport`（play/stop/seek/loop）、`state`、`shutdown`；`revision` 单调递增，断档拒绝 `-32001`。Python 客户端 `tsov/host/host_client.py` 支持启动/握手/状态/传输/崩溃重启；`tsov/host/live.py` 提供 Score → graph 与 `LiveBridge`。S2.4 起 manifest 支持 VST3 与 SF2，`transport.loop` 在音频回调内按采样边界回卷，`state` 带输出延迟/loop 区间。

## 目标

Python（`tsov/host/host_client.py`）与 sidecar（`tsov-host.exe`）之间的最小控制面。音频线程不读 socket；网络线程解析后投递无锁队列。

## 握手

客户端连接后必须首先发送：

```json
{"jsonrpc":"2.0","id":1,"method":"hello","params":{"protocol":0,"client":"tsov-python"}}
```

服务端回：

```json
{"jsonrpc":"2.0","id":1,"result":{"protocol":0,"host":"tsov-host","version":"0.1.0","sampleRate":48000,"blockSize":128,"outputLatencySamples":0}}
```

协议版本不匹配 → 服务端回 `error.code=-32000` 并关闭连接。

## 方法（v0）

| 方法 | 参数 | 返回 | 说明 |
|---|---|---|---|
| `hello` | `protocol`, `client` | 版本/设备信息 | 必须首先调用 |
| `load_graph` | `revision`, `graph.tracks[]` | `revision` | 全量装载；按现存 sidecar 轨序应用 notes/gain/pan/mute；轨数超出 manifest 拒绝（需重启） |
| `patch` | `revision`, `ops[]`（`set_notes` / `set_track_mix`） | `revision` | E1 命令级增量；revision 断档回 `-32001` |
| `transport` | `action`（play/stop/seek/loop）；`seek` 用 `position` 样本；`loop` 用 `start`/`end` 样本，`end<=start` 清除 | `ok` | 实时传输控制 |
| `state` | — | `playhead`, `xruns`, `outputLatencySamples`, `loopStart`, `loopEnd`, `tracks[].peak`, `revision` | 已实现；30Hz 推送待做 |
| `shutdown` | `graceful` | `ok` | 停引擎并退出 |

## 通知（服务端 → 客户端）

```json
{"jsonrpc":"2.0","method":"state","params":{"playhead":12.345,"xruns":0,"cpu":0.12,"revision":42}}
{"jsonrpc":"2.0","method":"error","params":{"code":-32002,"message":"plugin crashed","track":3}}
```

## 约束

- 只绑定 `127.0.0.1`；v0 不做鉴权（本机单用户），S1 起加临时 token。
- `revision` 单调递增；`patch` 以 expected revision 对账，断档/重复回 `-32001`（v0 不做幂等重放）。
- 音频线程只消费已排序事件队列；不得在回调内做 JSON/分配/锁。
- 崩溃/重启后客户端重连 → `hello` → 全量恢复；当前由 `HostClient.restart()` 重新拉起同一 manifest 并重载每轨 VST3 state。
- **活图 v0（S2.2）**：`graph = {sample_rate, tempo, time_signature, tracks:[{id,name,notes:[{start,end,pitch_midi,velocity}],gain,pan,mute}]}`；notes 为**秒**，sidecar 按实际采样率转样本。`load_graph` 以 sidecar 现存轨序为准，插件实例/state 仍由 manifest 启动时加载；`patch` 的 op 只影响未来触发（播放中编辑不重触发已发声音符）。
- **manifest（S2.4）**：VST3 轨 `{"type":"vst3","plugin":"...","state":"...","gain":0.8,"pan":0}`；SF2 轨 `{"type":"sf2","soundfont":"...","bank":0,"program":0}`（打击乐 bank 128）。SF2 由仓库自带 FluidSynth DLL 渲染；每轨一个 synth 实例（内存换实现简单，共享 bank 留后续）。
- **C1 延迟补偿**：`outputLatencySamples` 是设备输出延迟；Web 侧 SSE 的 `playhead` 已减去该延迟（秒），使播放头对齐实际听到的位置。回环区间由 sidecar 音频回调按采样边界回卷，Python 不再做 30Hz 回卷。
