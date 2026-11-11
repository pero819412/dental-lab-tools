"""ファイルベースの IPC（変換依頼キュー・イベント・常駐サービス状態）。

別プロセス（例: 受注監視ツール）が enqueue_encode() で依頼 JSON を置き、
常駐サービス側が claim_requests() → 変換 → finish_request() で処理する想定。
各操作は「一時ファイルへ書く → os.replace で確定」の原子的書き込みで行う。

置き場所は settings.ipc_root()（既定: <データ置き場>/ipc）。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from . import settings as _settings


def _ipc_root() -> Path:
    return _settings.ipc_root()


def _request_dir() -> Path:
    return _ipc_root() / "requests"


def _processing_dir() -> Path:
    return _ipc_root() / "processing"


def _event_dir() -> Path:
    return _ipc_root() / "events"


def _status_path() -> Path:
    return _ipc_root() / "service_status.json"


def lock_path() -> Path:
    return _ipc_root() / "service.lock"


def _ensure_directories() -> None:
    for path in (_ipc_root(), _request_dir(), _processing_dir(), _event_dir()):
        path.mkdir(parents=True, exist_ok=True)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def load_encoder_settings() -> dict[str, Any]:
    return _settings.load_settings()


def save_encoder_settings(settings: dict[str, Any]) -> dict[str, Any]:
    current = _settings.load_settings()
    current.update(settings)
    return _settings.save_settings(current)


def enqueue_encode(
    folder: str | os.PathLike[str],
    source: str = "external",
    material_override: str = "",
) -> str:
    _ensure_directories()
    request_id = uuid.uuid4().hex
    payload = {
        "id": request_id,
        "action": "encode",
        "folder": os.path.normpath(os.fspath(folder)),
        "source": source,
        "material_override": str(material_override or "").strip().upper(),
        "created_at": time.time(),
    }
    _atomic_write_json(_request_dir() / f"{time.time_ns()}_{request_id}.json", payload)
    return request_id


def recover_processing_requests() -> None:
    _ensure_directories()
    for path in _processing_dir().glob("*.json"):
        target = _request_dir() / path.name
        try:
            os.replace(path, target)
        except OSError:
            continue


def claim_requests(limit: int = 8) -> list[tuple[Path, dict[str, Any]]]:
    _ensure_directories()
    claimed: list[tuple[Path, dict[str, Any]]] = []
    for source_path in sorted(_request_dir().glob("*.json"))[: max(1, limit)]:
        claimed_path = _processing_dir() / source_path.name
        try:
            os.replace(source_path, claimed_path)
            with claimed_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, dict):
                raise ValueError("依頼データが辞書形式ではありません")
            claimed.append((claimed_path, payload))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            write_event("error", f"変換依頼を読み込めませんでした: {source_path.name} / {error}")
            try:
                claimed_path.unlink(missing_ok=True)
            except OSError:
                pass
    return claimed


def finish_request(claimed_path: Path) -> None:
    try:
        claimed_path.unlink(missing_ok=True)
    except OSError:
        pass


def write_event(
    level: str,
    message: str,
    *,
    request_id: str = "",
    kind: str = "log",
) -> Path:
    _ensure_directories()
    event_id = uuid.uuid4().hex
    payload = {
        "id": event_id,
        "request_id": request_id,
        "level": str(level or "info").lower(),
        "kind": kind,
        "message": str(message),
        "created_at": time.time(),
    }
    path = _event_dir() / f"{time.time_ns()}_{event_id}.json"
    _atomic_write_json(path, payload)
    return path


def drain_events(limit: int = 100) -> list[dict[str, Any]]:
    _ensure_directories()
    events: list[dict[str, Any]] = []
    for path in sorted(_event_dir().glob("*.json"))[: max(1, limit)]:
        claimed = path.with_suffix(".reading")
        try:
            os.replace(path, claimed)
            with claimed.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                events.append(payload)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        finally:
            try:
                claimed.unlink(missing_ok=True)
            except OSError:
                pass
    return events


def update_service_status(**values: Any) -> None:
    status = {
        "pid": os.getpid(),
        "heartbeat": time.time(),
    }
    status.update(values)
    _atomic_write_json(_status_path(), status)


def read_service_status() -> dict[str, Any]:
    try:
        with _status_path().open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except (FileNotFoundError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def service_is_alive(max_age_seconds: float = 8.0) -> bool:
    status = read_service_status()
    if str(status.get("state") or "").lower() == "stopped":
        return False
    try:
        return time.time() - float(status.get("heartbeat", 0)) <= max_age_seconds
    except (TypeError, ValueError):
        return False


def prune_events(max_age_days: int = 7) -> None:
    _ensure_directories()
    cutoff = time.time() - max(1, max_age_days) * 86400
    for path in _event_dir().iterdir():
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue
