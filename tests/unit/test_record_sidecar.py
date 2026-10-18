"""S2.9: Web 录音 sidecar 适配层（不打开真实设备）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tsov.webapp import record_sidecar as rs


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, manifest, **kwargs):
        self.manifest = Path(manifest)
        self.kwargs = kwargs
        self.started = False
        self.stopped = False
        self.recording = False
        self.path = ""
        FakeClient.instances.append(self)

    def start(self):
        assert self.manifest.exists()
        self.started = True
        return {"host": "fake"}

    def record_start(self, path, channels=1):
        self.path = str(path)
        self.recording = True
        return {"ok": True, "path": self.path}

    def record_status(self):
        return {"recording": self.recording, "recordedFrames": 44100 if self.recording else 0,
                "dropouts": 0, "inputPeak": 0.25}

    def record_stop(self):
        self.recording = False
        return {"ok": True, "recordedFrames": 44100, "dropouts": 0}

    def stop(self):
        self.stopped = True


def test_record_sidecar_start_status_stop(tmp_path, monkeypatch):
    FakeClient.instances.clear()
    monkeypatch.setattr(rs, "HostClient", FakeClient)
    project = SimpleNamespace(name="demo", root=tmp_path / "projects" / "demo")
    project.root.mkdir(parents=True)
    target = project.root / "audio" / ".incoming" / "take.wav"
    side = rs.RecordSidecar()
    try:
        info = side.start(project, target, device="GP-50", monitor=True)
        assert info["device"] == "GP-50"
        assert side.active
        client = FakeClient.instances[0]
        assert client.kwargs["input_channels"] == 2
        assert client.kwargs["monitor"] is True
        assert client.manifest.read_text() == "[]"
        status = side.status()
        assert status["active"] is True
        assert status["recordedFrames"] == 44100
        result = side.stop()
        assert result["recordedFrames"] == 44100
        assert result["path"] == str(target)
        assert client.stopped is True
        assert not client.manifest.exists()
        assert side.active is False
    finally:
        side.close()


def test_record_sidecar_rejects_second_start(tmp_path, monkeypatch):
    FakeClient.instances.clear()
    monkeypatch.setattr(rs, "HostClient", FakeClient)
    project = SimpleNamespace(name="demo", root=tmp_path / "projects" / "demo")
    project.root.mkdir(parents=True)
    side = rs.RecordSidecar()
    try:
        side.start(project, project.root / "a.wav", device="GP-50")
        with pytest.raises(RuntimeError):
            side.start(project, project.root / "b.wav", device="GP-50")
    finally:
        side.close()
