# tsov-host（实时宿主 sidecar）

S0 骨架：JUCE 8.0.9 控制台宿主，负责实时音频/插件（后续 S1 起接入设备、图、VST3、录音）。

## 构建（Windows x64）

```bash
cd "D:/My Project/the shape of voice"

# 首次配置会下载 JUCE 8.0.9 到 build/_deps/juce-src
tsov/.venv/Scripts/cmake.exe -S native/tsov-host -B native/tsov-host/build \
  -G "Visual Studio 17 2022" -A x64

tsov/.venv/Scripts/cmake.exe --build native/tsov-host/build --config Release
```

## 冒烟

```bash
native/tsov-host/build/bin/Release/tsov-host.exe --selftest
native/tsov-host/build/bin/Release/tsov-host.exe --selftest --device null
native/tsov-host/build/bin/Release/tsov-host.exe --list-devices
native/tsov-host/build/bin/Release/tsov-host.exe --run --duration 2
native/tsov-host/build/bin/Release/tsov-host.exe --run --device null --duration 1 --tone 220
native/tsov-host/build/bin/Release/tsov-host.exe --scan-vst3
native/tsov-host/build/bin/Release/tsov-host.exe --plugin-test "vendor/vst3/Dexed.vst3" --sample-rate 48000 --block-size 128
native/tsov-host/build/bin/Release/tsov-host.exe --plugin-live "vendor/vst3/Dexed.vst3" --device "GP-50" --device-type "Exclusive" --duration 2
native/tsov-host/build/bin/Release/tsov-host.exe --plugin-editor "vendor/vst3/Dexed.vst3" --device "GP-50" --device-type "Exclusive" --duration 3
native/tsov-host/build/bin/Release/tsov-host.exe --mix-test "vendor/vst3/Dexed.vst3" --tracks 2 --device "GP-50" --device-type "Exclusive" --duration 2
native/tsov-host/build/bin/Release/tsov-host.exe --mix-test "vendor/vst3/Dexed.vst3" --tracks 2 --device null --duration 1
native/tsov-host/build/bin/Release/tsov-host.exe --mix-test "vendor/vst3/Dexed.vst3" --tracks 1 --record "output/host-record.wav" --device "GP-50" --device-type "Windows Audio" --duration 3
native/tsov-host/build/bin/Release/tsov-host.exe --plugin-editor "C:/Program Files/Common Files/VST3/SSDSampler5.vst3" --device "GP-50" --device-type "Exclusive" --duration 120 --note 36 --state "output/ssd5-free.state"
native/tsov-host/build/bin/Release/tsov-host.exe --plugin-live "C:/Program Files/Common Files/VST3/SSDSampler5.vst3" --device "GP-50" --device-type "Exclusive" --duration 3 --note 36 --state "output/ssd5-free.state"
native/tsov-host/build/bin/Release/tsov-host.exe --plugin-live "C:/Program Files/Common Files/VST3/Kontakt 8.vst3" --device "GP-50" --device-type "Exclusive" --duration 3 --note 60 --state "output/kontakt-factory.state"
native/tsov-host/build/bin/Release/tsov-host.exe --mix-manifest "output/mix-kontakt-ssd5-surge.json" --device "GP-50" --device-type "Exclusive" --duration 3
native/tsov-host/build/bin/Release/tsov-host.exe --version
```

期望：`juce=8.0.9`、`plugin_formats=1`（VST3）、`selftest=ok`；`--plugin-test` 输出 `plugin_test=ok`；`--plugin-live` / `--plugin-editor` 输出 `run=ok` / `editor=ok`，`xruns=0`（本机 GP-50 实际格式为 44.1k / 132 帧，见下）；`--mix-test --tracks 2` 输出 `mix=ok`、两轨 peak 非零。

## 多轨与监听

- `MixEngine` 已支持 N 轨 VST3、每轨 gain/pan/mute、采样级 MIDI 事件、master 总线；`--mix-test` 用同一插件起多实例验证。
- `--mix-manifest <tracks.json>`：每轨独立 `plugin` / `state` / `gain` / `pan` / `note`；实测 Kontakt + SSD5 两轨、再加 Surge XT 三轨均 **0 xrun**，各轨 peak 独立。
- 监听/录音输入请用 **Windows Audio（共享）**：GP-50 在 Exclusive 下「输入+输出」同时开会周期性 xrun（实测 10s 24 次）；共享模式 10s 仅 1 次。纯播放仍用 Exclusive。
- 输入峰值见 `input_peak`；无输入信号时为 0 属正常。

## 设备格式

请求格式（`--sample-rate/--block-size`）会按设备支持列表取最近值；本机 Valeton GP-50 经 Windows Audio 仅支持 **44100 Hz**，最小可用缓冲 132 帧（≈3ms）——48k/128 需 ASIO 驱动或换设备。

## 约定

- 构建目录 `build/` 不入库（`.gitignore` 的 `build/`）。
- IPC 契约 v0：`docs/ipc-v0.md`（S1 实现服务端）。
- 许可与工具链决策：`docs/decisions/0026-实时宿主JUCE与工具链.md`。
- 本目录是 C++ 侧；Python 控制面在 `tsov/host/host_client.py`（S2 起）。
