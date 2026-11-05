"""Inbox-drop sensor: every *.md / *.txt dropped into `paths.inbox_drop` becomes a signal, then the file is
moved to `processed/<timestamp>_<name>`. This is how the user (or another tool) pushes a request into AWOS."""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from ..config import load_config
from ..repo import fingerprint
from ._common import local_dt, redact, trunc, utc_iso
from .base import Sensor, SensorContext, SignalDraft

EXTS = {".md", ".txt"}
SETTLE_S = 5.0            # only take files whose mtime is at least this old (writer finished)
MAX_BYTES = 1 << 20


def _read(p: Path) -> str:
    raw = p.read_bytes()[:MAX_BYTES]
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _title(text: str, fallback: str) -> str:
    for line in text.splitlines():
        s = line.strip().lstrip("#").strip()
        if s and s != "---":
            return trunc(s, 120)
    return fallback


class InboxDropSensor(Sensor):
    name = "inbox_drop"

    def _dir(self, ctx: SensorContext) -> Path:
        d = ctx.config.get("dir")
        return Path(d) if d else load_config().path("inbox_drop")

    def collect(self, ctx: SensorContext) -> list[SignalDraft]:
        inbox = self._dir(ctx)
        inbox.mkdir(parents=True, exist_ok=True)
        done = inbox / "processed"
        out: list[SignalDraft] = []
        now = time.time()
        seen: dict[Path, tuple[int, float]] = {}
        for p in sorted(inbox.iterdir(), key=lambda p: p.name):
            if p.is_file() and p.suffix.lower() in EXTS and not p.name.startswith((".", "~")):
                st = p.stat()
                if now - st.st_mtime >= SETTLE_S:
                    seen[p] = (st.st_size, st.st_mtime)
        for p, (size, mtime) in seen.items():
            try:
                st = p.stat()
            except OSError:
                continue
            if (st.st_size, st.st_mtime) != (size, mtime):  # still being written: next run
                continue
            text = _read(p)
            body = redact(text.strip())
            truncated = len(body) > 4000
            out.append(SignalDraft(
                title=_title(text, p.stem), fingerprint=fingerprint("inbox_drop", p.name, int(st.st_mtime), text),
                body=body[:4000], url=None, external_id=p.name, occurred_at=utc_iso(st.st_mtime),
                payload={"filename": p.name, "size": st.st_size, "truncated": truncated,
                         "dropped_at": local_dt(st.st_mtime).strftime("%Y-%m-%d %H:%M")}))
            done.mkdir(exist_ok=True)
            dest = done / f"{local_dt(now).strftime('%Y%m%d-%H%M%S')}_{p.name}"
            n = 1
            while dest.exists():
                dest = done / f"{local_dt(now).strftime('%Y%m%d-%H%M%S')}-{n}_{p.name}"
                n += 1
            shutil.move(str(p), str(dest))
            out[-1].payload["processed_path"] = str(dest)
        return out

    def context_snapshot(self, ctx: SensorContext) -> str | None:
        return None


SENSOR = InboxDropSensor()
