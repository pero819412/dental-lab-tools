"""設定ファイルとデータ置き場の解決（app.py と ipc.py の共通部分）。

保存場所（優先順）:
  1. 環境変数 HDPP_ENCODER_CONFIG … 設定 JSON のフルパス
  2. 環境変数 HDPP_ENCODER_HOME   … データ置き場（settings.json / logs / ipc）
  3. Windows: %APPDATA%\\hdpp_encoder、その他: ~/.config/hdpp_encoder

出力先・監視先には既定値を持たない。未設定のまま変換・自動監視を始めると
エラーにする（誤った場所へ書き込まないため）。
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from .core import clean_path, clean_staff_name, configure_material_names

# config.example.json に書いてあるダミー値。これが残っていたら未設定扱いにする。
PLACEHOLDER_OUTPUT_PATH = r"\\SERVER\Share\Output"
PLACEHOLDER_WATCH_PATH = r"C:\path\to\exocad\output"
PLACEHOLDER_PATHS = {PLACEHOLDER_OUTPUT_PATH.casefold(), PLACEHOLDER_WATCH_PATH.casefold()}

DEFAULT_SETTINGS: dict[str, Any] = {
    "output_path": "",
    "watch_path": "",
    "staff_name": "",
    "auto_enabled": False,
    "material_names": {},
}


def data_home() -> Path:
    explicit = os.environ.get("HDPP_ENCODER_HOME", "").strip()
    if explicit:
        return Path(explicit)
    appdata = os.environ.get("APPDATA", "").strip()
    if os.name == "nt" and appdata:
        return Path(appdata) / "hdpp_encoder"
    return Path.home() / ".config" / "hdpp_encoder"


def config_path() -> Path:
    explicit = os.environ.get("HDPP_ENCODER_CONFIG", "").strip()
    if explicit:
        return Path(explicit)
    return data_home() / "settings.json"


def log_dir() -> Path:
    return data_home() / "logs"


def ipc_root() -> Path:
    return data_home() / "ipc"


def is_configured_path(value: object) -> bool:
    text = clean_path(value)
    return bool(text) and text.casefold() not in PLACEHOLDER_PATHS


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def load_settings() -> dict[str, Any]:
    settings = {**DEFAULT_SETTINGS, "material_names": {}}
    try:
        with config_path().open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            for key in ("output_path", "watch_path"):
                value = clean_path(loaded.get(key))
                if is_configured_path(value):
                    settings[key] = value
            settings["staff_name"] = clean_staff_name(loaded.get("staff_name"))
            settings["auto_enabled"] = bool(loaded.get("auto_enabled", False))
            names = loaded.get("material_names")
            if isinstance(names, dict):
                settings["material_names"] = {str(k): str(v) for k, v in names.items()}
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        logging.warning("設定ファイルを読み込めませんでした。既定値を使用します: %s", error)
    configure_material_names(settings["material_names"])
    return settings


def save_settings(settings: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "output_path": clean_path(settings.get("output_path")),
        "watch_path": clean_path(settings.get("watch_path")),
        "staff_name": clean_staff_name(settings.get("staff_name")),
        "auto_enabled": bool(settings.get("auto_enabled", False)),
        "material_names": dict(settings.get("material_names") or {}),
    }
    payload = (json.dumps(normalized, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    atomic_write_bytes(config_path(), payload)
    return normalized
