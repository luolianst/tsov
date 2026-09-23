"""音频素材 / 录音域路由（M-V8 E2；F5 自 tsov/web.py 拆出）。

- audio/import（JSON 路径或 multipart 上传）/ peaks / file
- record：设备枚举 / 起停 / 入库 + 命令层入轨（全局单录音 `_recorder`）
函数体逐行保留。
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

from ...host import EditBatch
from ..helpers import _resolve_project_audio, project_state
from ..record import _recorder
from ..state import WebState


def register(app: FastAPI) -> None:
    def st() -> WebState:
        return app.state.tsov

    # ---------------- 音频素材（M-V8 E2：音频轨·第一刀） ----------------

    @app.post("/api/projects/{name}/audio/import")
    async def audio_import(name: str, request: Request) -> dict:
        """音频入库（E2）：JSON {path: 本机绝对路径, name?} 或 multipart file 上传。

        - 路径来源：后端直读本机文件（原曲素材场景，仅本机服务）
        - 上传来源：浏览器 file input / 拖拽 → 落临时文件后同路径入库（ffmpeg 转 44.1k flac）
        """
        proj = st().get_project(name)
        ctype = (request.headers.get("content-type") or "").lower()
        tmp_path = None
        try:
            if "multipart/form-data" in ctype:
                form = await request.form()
                up = form.get("file")
                if up is None or not getattr(up, "filename", ""):
                    raise HTTPException(400, "multipart 缺 file 字段")
                up_name = str(form.get("name") or "").strip() or None
                suffix = Path(str(up.filename)).suffix or ".bin"
                incoming = proj.root / "audio" / ".incoming"
                incoming.mkdir(parents=True, exist_ok=True)
                tmp_path = incoming / f"upload-{uuid.uuid4().hex[:8]}{suffix}"
                tmp_path.write_bytes(await up.read())
                info = proj.import_audio(tmp_path, name=up_name)
            else:
                body = await request.json()
                src = str((body or {}).get("path") or "").strip().strip('"').strip("'")
                if not src:
                    raise HTTPException(400, "缺 path（本机音频绝对路径）")
                info = proj.import_audio(src, name=(body or {}).get("name"))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
        return {"project": name, **info}

    @app.get("/api/projects/{name}/audio/peaks")
    def audio_peaks(name: str, file: str, buckets: int = 800) -> dict:
        """波形峰值（E2）：mono 降采样 min/max 桶数组 → 前端 canvas 绘制。"""
        import soundfile as sf

        proj = st().get_project(name)
        path = _resolve_project_audio(proj, file)
        buckets = max(8, min(4000, int(buckets)))
        data, sr = sf.read(str(path), dtype="float32", always_2d=True)
        mono = data.mean(axis=1) if data.shape[1] > 1 else data[:, 0]
        n = int(mono.shape[0])
        per = max(1, n // buckets)
        m = (n // per) * per
        if m >= per:
            blk = mono[:m].reshape(-1, per)
            mins = blk.min(axis=1)
            maxs = blk.max(axis=1)
            if m < n:  # 尾部余量并入末桶
                mins[-1] = min(float(mins[-1]), float(mono[m:].min()))
                maxs[-1] = max(float(maxs[-1]), float(mono[m:].max()))
        else:
            mins, maxs = mono[:1], mono[:1]
        return {
            "file": path.name,
            "seconds": round(n / int(sr), 4),
            "buckets": int(len(mins)),
            "min": [round(float(x), 5) for x in mins],
            "max": [round(float(x), 5) for x in maxs],
        }

    @app.get("/api/projects/{name}/audio/file")
    def audio_file(name: str, file: str):
        """试听/交付工程内音频文件（FileResponse）。"""
        from fastapi.responses import FileResponse

        proj = st().get_project(name)
        path = _resolve_project_audio(proj, file)
        media = "audio/flac" if path.suffix.lower() == ".flac" else "application/octet-stream"
        return FileResponse(str(path), media_type=media, filename=path.name)

    # ---- 录音（M-V8 E2 段 3）：设备枚举 / 起停 / 入库 + 命令层入轨 ----

    @app.get("/api/record/devices")
    def record_devices() -> dict:
        """录音设备清单：输入（含默认标记）+ 输出（监听用）。"""
        import sounddevice as sd

        from ...host.record import list_input_devices

        outs = [
            {"index": i, "name": str(d.get("name", "")),
             "channels": int(d.get("max_output_channels", 0))}
            for i, d in enumerate(sd.query_devices())
            if int(d.get("max_output_channels", 0)) > 0
        ]
        try:
            din, dout = sd.default.device[0], sd.default.device[1]
        except Exception:  # noqa: BLE001 设备枚举异常时不给默认
            din = dout = None
        return {"inputs": list_input_devices(), "outputs": outs,
                "default_in": din, "default_out": dout}

    @app.post("/api/projects/{name}/record/start")
    def record_start(name: str, body: dict | None = None) -> dict:
        """开始录制（body: device/out_device/monitor/samplerate；一次一个）。"""
        body = body or {}
        st().get_project(name)  # 工程存在性校验（不存在 → 404）
        with _recorder.lock:
            if _recorder.thread is not None and _recorder.thread.is_alive():
                raise HTTPException(409, "已有录音进行中（先停止）")
            device = body.get("device")
            out_device = body.get("out_device")
            monitor = bool(body.get("monitor"))
            samplerate = int(body.get("samplerate") or 48000)
            proj = st().get_project(name)
            incoming = proj.root / "audio" / ".incoming"
            incoming.mkdir(parents=True, exist_ok=True)
            tmp = incoming / f"rec-{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}.wav"
            stop_event = threading.Event()
            _recorder.stop_event = stop_event
            _recorder.project = name
            _recorder.started_at = time.time()
            _recorder.report = None
            _recorder.error = None

            def _run() -> None:
                try:
                    from ...host.record import record_with_monitor

                    _recorder.report = record_with_monitor(
                        tmp, device=device, out_device=out_device, monitor=monitor,
                        samplerate=samplerate, stop_event=stop_event,
                    )
                except Exception as e:  # noqa: BLE001 线程内任何异常记入 error
                    _recorder.error = f"{type(e).__name__}: {e}"

            th = threading.Thread(target=_run, daemon=True, name="tsov-record")
            _recorder.thread = th
            th.start()
        return {"recording": True, "monitor": monitor, "device": device,
                "out_device": out_device, "samplerate": samplerate,
                "started_at": _recorder.started_at}

    @app.get("/api/projects/{name}/record/status")
    def record_status(name: str) -> dict:
        """录音状态（前端计时/轮询）。"""
        st().get_project(name)
        th = _recorder.thread
        alive = th is not None and th.is_alive()
        return {
            "project": _recorder.project,
            "recording": alive,
            "elapsed": round(time.time() - _recorder.started_at, 2)
            if alive and _recorder.started_at else None,
            "error": _recorder.error,
        }

    @app.post("/api/projects/{name}/record/stop")
    def record_stop(name: str, body: dict | None = None) -> dict:
        """停止录音 → 入库（44.1k flac + 去重）→ 命令层 add_audio_track（默认加轨）。"""
        body = body or {}
        proj = st().get_project(name)
        with _recorder.lock:
            th = _recorder.thread
            if th is None or not th.is_alive():
                raise HTTPException(409, "当前没有进行中的录音")
            if _recorder.stop_event is not None:
                _recorder.stop_event.set()
        th.join(timeout=20)
        if th.is_alive():
            raise HTTPException(500, "录音线程未停止（设备卡死？）")
        if _recorder.error:
            err = _recorder.error
            _recorder.thread = None
            raise HTTPException(500, f"录制失败：{err}")
        rep = _recorder.report or {}
        if int(rep.get("frames") or 0) <= 0:
            _recorder.thread = None
            raise HTTPException(400, "没录到音频（设备无信号或权限被拒）")

        src = Path(str(rep.get("path")))
        try:
            info = proj.import_audio(src, name=body.get("name"))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        finally:
            try:
                src.unlink(missing_ok=True)  # 临时 wav 用完即删（内容已转 flac 入库）
            except OSError:
                pass

        result = None
        if body.get("add_track", True):
            batch = EditBatch(label="录音入库").add(
                "add_audio_track",
                value={"file": info["file"], "name": body.get("name") or "录音"})
            result = proj.apply_batch(batch, commit_message="录音入库", source="user")
            if result.get("applied"):
                st().bus.publish(name, "diff_applied",
                                 {**result["diff"], "commit": result.get("commit"),
                                  "seq": result.get("seq")})
                st().bus.publish(name, "state_updated", project_state(proj))
        with _recorder.lock:
            _recorder.thread = None  # 释放（可再次录制）
        return {"recorded": rep, "file": info["file"], "seconds": info["seconds"],
                "deduped": info["deduped"],
                "added": bool(result and result.get("applied")), "batch": result}
