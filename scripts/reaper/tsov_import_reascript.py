# -*- coding: utf-8 -*-
# tsov_import_reascript.py
# Reaper ReaScript: 把 tsov 的 MIDI 产物导入 Reaper 并挂 ReaSynth 播放（最小闭环）
# 运行方式：Reaper Actions -> ReaScript: Run -> 选本文件（Python）
# 操作日志：写入 output\reaper-script-log.txt（远程验证用）
import os
import sys
import json
import time
import datetime

PROJECT_ROOT = r"<repo>"
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")
LOG_PATH = os.path.join(OUTPUT_DIR, "reaper-script-log.txt")
INITIAL_DIR = OUTPUT_DIR

# 允许在 Reaper 外 mock 测试
sys.path.insert(0, PROJECT_ROOT)

_NS = globals()


def _log(msg):
    line = "[%s] %s" % (
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        msg,
    )
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass
    try:
        RPR_ShowConsoleMsg(line + "\n")
    except Exception:
        pass


def _call(name, *args):
    """调用 RPR_* 函数；不存在时抛异常让外层兜底。"""
    fn = _NS.get(name)
    if fn is None:
        raise RuntimeError("API missing: %s" % name)
    return fn(*args)


def _add_rea_synth(track):
    """挂 ReaSynth。名称匹配实测用多个候选。返回 (index, 命中的名称)。"""
    candidates = [
        "ReaSynth (Cockos)",
        "VST: ReaSynth (Cockos)",
        "VST3: ReaSynth (Cockos)",
        "ReaSynth",
    ]
    for name in candidates:
        try:
            idx = _call("RPR_TrackFX_AddByName", track, name, 0, -1)
        except Exception as e:
            _log("TrackFX_AddByName(%s) exception: %s" % (name, e))
            continue
        # 返回 -1 表示未找到；非负表示成功（wantindex=-1 时返回插入的 FX 下标）
        if idx is not None and int(idx) >= 0:
            return int(idx), name
        _log("TrackFX_AddByName(%s) -> %s (not found)" % (name, idx))
    return -1, None


def _insert_midi(midi_file):
    """把 MIDI 插入当前选中轨道。先试 mode=1（插入选中轨），失败退回 mode=0。"""
    for mode in (1, 0):
        try:
            res = _call("RPR_InsertMedia", midi_file, mode)
            _log("InsertMedia(mode=%d) -> %s" % (mode, res))
        except Exception as e:
            _log("InsertMedia(mode=%d) exception: %s" % (mode, e))
            continue
        # 验证轨道上是否真的有 item
        n = _call("RPR_CountTrackMediaItems", _NS.get("_CURRENT_TRACK"))
        if n and int(n) > 0:
            return True, mode
    return False, None


def _add_phrase_markers(midi_file):
    """加分项：读 MIDI 同目录的 stage-01-voice.json 段信息，时间线标乐句区域。"""
    d = os.path.dirname(midi_file)
    voice_json = os.path.join(d, "stage-01-voice.json")
    score_json = os.path.join(d, "stage-04-score.json")
    src = None
    for cand in (voice_json, score_json):
        if os.path.exists(cand):
            src = cand
            break
    if not src:
        _log("markers: no stage-*.json beside MIDI, skipped")
        return 0
    try:
        with open(src, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "segments" in data and isinstance(data["segments"], list):
            segs = data["segments"]
        elif "tracks" in data:
            # score json 无 segments，跳过
            segs = []
        else:
            segs = []
        count = 0
        for i, s in enumerate(segs):
            start = s.get("start", 0.0)
            end = s.get("end", start + 1.0)
            rgnend = max(float(end), float(start) + 0.01)
            try:
                _call("RPR_AddProjectMarker", 0, 1, float(start), rgnend, "phrase-%d" % (i + 1), -1)
                count += 1
            except Exception as e:
                _log("AddProjectMarker(%s) exception: %s" % (s, e))
        _log("markers: added %d phrase regions from %s" % (count, os.path.basename(src)))
        return count
    except Exception as e:
        _log("markers: parse error %s" % e)
        return 0


def _play():
    """开始播放。transport_Play 在本版 Python 绑定中不存在，用 OnPlayButton。"""
    try:
        state = int(_call("RPR_GetPlayState"))
    except Exception:
        state = -1
    _log("play state before: %s" % state)
    # 0=stopped 1=playing 2=paused 4=record
    if state in (0, 2):
        try:
            _call("RPR_OnPlayButton")
            _log("play: OnPlayButton called")
        except Exception as e:
            _log("play: OnPlayButton exception %s" % e)
    else:
        _log("play: already playing/recording, skip")
    time.sleep(0.2)
    try:
        after = int(_call("RPR_GetPlayState"))
    except Exception:
        after = -1
    _log("play state after: %s" % after)
    return after


def main():
    _log("=== tsov import ReaScript start ===")
    _log("python version: %s" % sys.version.split()[0])

    # 1) 文件选择对话框（初始目录 = output\）
    try:
        ret, filename, _, _ = _call("RPR_GetUserFileNameForRead", INITIAL_DIR, "Select tsov MIDI file", "mid")
    except Exception as e:
        _log("GetUserFileNameForRead exception: %s" % e)
        return
    _log("dialog ret=%s filename=%s" % (ret, filename))
    if not ret or not filename:
        _log("user cancelled")
        return
    if not os.path.isfile(filename):
        _log("ERROR: file not found: %s" % filename)
        return
    if not filename.lower().endswith((".mid", ".midi")):
        _log("WARN: not a .mid file: %s" % filename)

    # 2) 新建轨道（插到最前）
    try:
        _call("RPR_InsertTrackAtIndex", 0, 1)
    except Exception as e:
        _log("InsertTrackAtIndex exception: %s" % e)
        return
    try:
        track = _call("RPR_GetTrack", 0, 0)
    except Exception as e:
        _log("GetTrack exception: %s" % e)
        return
    if not track:
        _log("ERROR: new track is None")
        return
    _NS["_CURRENT_TRACK"] = track
    _log("track created: %s" % track)

    # 命名轨道
    name = "tsov melody"
    try:
        _call("RPR_GetSetMediaTrackInfo_String", track, "P_NAME", name, True)
        _log("track named: %s" % name)
    except Exception as e:
        _log("set track name exception: %s" % e)

    # 3) 选中该轨道并把编辑光标移到 0，再导入 MIDI
    try:
        _call("RPR_SetOnlyTrackSelected", track)
        _call("RPR_SetEditCurPos", 0, 0, 0)
    except Exception as e:
        _log("select/cursor exception: %s" % e)
    ok, mode = _insert_midi(filename)
    if not ok:
        _log("ERROR: InsertMedia failed, no media item on track")
        return
    _log("MIDI inserted (mode=%d)" % mode)
    try:
        n = _call("RPR_CountTrackMediaItems", track)
        _log("media items on track: %d" % int(n))
    except Exception as e:
        _log("count items exception: %s" % e)

    # 4) 挂 ReaSynth
    fx_idx, fx_name = _add_rea_synth(track)
    if fx_idx >= 0:
        _log("ReaSynth added: index=%d name=%s" % (fx_idx, fx_name))
    else:
        _log("ERROR: ReaSynth NOT added (none of the candidate names matched)")

    # 加分项：乐句 markers
    try:
        _add_phrase_markers(filename)
    except Exception as e:
        _log("markers exception: %s" % e)

    # 5) 开始播放（或提示按空格）
    try:
        _play()
    except Exception as e:
        _log("play exception: %s" % e)

    _log("=== tsov import ReaScript done ===")


if __name__ == "__main__":
    main()
