"""极简 .env 写入：保留注释和顺序，只更新指定 key。"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def write_env_values(path: Path, values: dict[str, str]) -> bool:
    """把 values 写回 .env（存在则替换，不存在则追加）。返回是否成功。"""
    if not values:
        return True
    path = Path(path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        seen: set[str] = set()
        output: list[str] = []
        for raw in lines:
            stripped = raw.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                output.append(raw)
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in values:
                output.append(f"{key}={values[key]}")
                seen.add(key)
            else:
                output.append(raw)
        if output and output[-1].strip():
            output.append("")
        for key, value in values.items():
            if key not in seen:
                output.append(f"{key}={value}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(output).rstrip("\n") + "\n", encoding="utf-8")
        for key, value in values.items():
            os.environ[key] = value
        return True
    except Exception as error:
        logger.warning("写入 .env 失败（%s）：%s", path, error)
        return False
