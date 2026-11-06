"""Shared helpers for Node-based agent runtimes (Pi, DSH) living under runtime/ with the portable Node."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..config import load_config
from ..osutil import IS_WINDOWS, bundled_or_path


def node_exe() -> Path | None:
    """Portable Node under paths.node_runtime (Windows zip: node.exe; macOS/Linux tarball: bin/node), else `node`
    on PATH (macOS/Linux only: on Windows the system Node is deliberately never used)."""
    base = load_config().root / load_config().get("paths.node_runtime", "runtime/node")
    return bundled_or_path(base / "node" if IS_WINDOWS else base / "bin" / "node", "node")


def runtime_dir(name: str) -> Path:
    return load_config().root / "runtime" / name


def runtime_secrets(name: str) -> dict[str, Any]:
    """[pi] / [dsh] sections of var/secrets.toml: env vars to inject (e.g. DEEPSEEK_API_KEY) + model."""
    return load_config().secrets().get(name, {}) or {}


SCHEMA_INSTRUCTION = """

---
## 输出格式（必须遵守）
完成后，你的**最后一条消息**必须只包含一个 JSON 对象（可以放在 ```json 代码块里），并严格符合下面的 JSON Schema，不要附加其它文字：
```json
{schema}
```"""


def schema_prompt(prompt: str, schema: dict | None) -> str:
    if not schema:
        return prompt
    return prompt + SCHEMA_INSTRUCTION.format(schema=json.dumps(schema, ensure_ascii=False, indent=1))


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def extract_json(text: str) -> dict | None:
    """Best-effort: last fenced JSON object, else the outermost {...} span."""
    if not text:
        return None
    for m in reversed(_FENCE.findall(text)):
        try:
            v = json.loads(m)
            if isinstance(v, dict):
                return v
        except ValueError:
            continue
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            v = json.loads(text[start:end + 1])
            return v if isinstance(v, dict) else None
        except ValueError:
            return None
    return None
