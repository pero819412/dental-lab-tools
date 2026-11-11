"""ドロップされたファイルのパース: Qt のドラッグ＆ドロップからローカルファイルの絶対パスだけを取り出す。

痒いところ: エクスプローラー・メーラー・ブラウザー・自作アプリなど、ドロップ元によってデータ形式
（file: URL / テキスト / Windows ネイティブの FileNameW・CF_HDROP）がバラバラで、取りこぼしや
相対パス・仮想添付の誤認が起きる。全形式を読み、重複を除いた絶対パスのリストに揃える。

Read filesystem drag payloads consistently, without accessing the filesystem.
Do not turn virtual attachment descriptors or web URLs into filesystem paths.
Existence/type validation remains the responsibility of each drop destination.

依存: PyQt6（QUrl と QMimeData / QDropEvent を受け取る前提）。
パス解釈は Windows 形式（ntpath）。"mbcs" コーデックは Windows でのみ使えるため、
FileName / ANSI の CF_HDROP は Windows 以外では読めずに無視される（URL・テキストは読める）。
"""
import logging
import ntpath
import re
import struct

from PyQt6.QtCore import QUrl

_LOGGER = logging.getLogger("file_drop_paths")
_NATIVE_FORMAT = re.compile(
    r'application/x-qt-windows-mime;\s*value="([^"]+)"', re.IGNORECASE
)
# 自アプリ内ドラッグで使う独自 MIME 形式（UTF-8、1行1パス）。必要に応じて名前を変える。
APP_PATH_FORMAT = "application/x-file-drop-paths"


def _absolute_path(value):
    value = str(value or "").strip().lstrip("﻿")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    if not value or "\x00" in value:
        return ""
    if value.lower().startswith("file:"):
        url = QUrl(value)
        if not url.isValid() or not url.isLocalFile():
            return ""
        if url.host().casefold() == "localhost":
            url.setHost("")
        value = url.toLocalFile()
    # A rooted path without a drive (\folder) and a drive-relative path (C:foo)
    # depend on this process's cwd and must not be accepted from other apps.
    drive, tail = ntpath.splitdrive(value)
    if re.fullmatch(r"[a-zA-Z]:", drive):
        if not tail.startswith(("\\", "/")):
            return ""
    elif drive.startswith(("\\\\", "//")):
        if value.startswith("\\\\.\\") or not drive.strip("\\/"):
            return ""
        if tail and not tail.startswith(("\\", "/")):
            return ""
    else:
        return ""
    return ntpath.normpath(value)


def _native_paths(raw, name):
    if name == "filenamew":
        return raw.decode("utf-16-le").split("\x00")
    if name == "filename":
        return raw.decode("mbcs").split("\x00")
    if name in {"cf_hdrop", "hdrop"}:
        if len(raw) < 20:
            return []
        offset, _x, _y, _nonclient, wide = struct.unpack_from("<IiiII", raw)
        if offset < 20 or offset >= len(raw) or wide not in {0, 1}:
            return []
        return raw[offset:].decode("utf-16-le" if wide else "mbcs").split("\x00")
    return []


def mime_file_paths(mime_data, app_path_format=APP_PATH_FORMAT):
    """Decode Qt URLs, text paths and native Windows file drops, deduplicated."""
    candidates = []
    if mime_data.hasUrls():
        for url in mime_data.urls():
            if url.isLocalFile():
                candidates.append(url.toString())
    if mime_data.hasText():
        candidates.extend(mime_data.text().replace("\x00", "\n").splitlines())
    for format_name in mime_data.formats():
        native = _NATIVE_FORMAT.match(format_name)
        native_name = native.group(1).casefold() if native else ""
        try:
            if native_name in {"filenamew", "filename", "cf_hdrop", "hdrop"}:
                candidates.extend(_native_paths(bytes(mime_data.data(format_name)), native_name))
            elif app_path_format and format_name == app_path_format:
                candidates.extend(bytes(mime_data.data(format_name)).decode("utf-8-sig").splitlines())
        except (UnicodeError, ValueError, LookupError, struct.error):
            # A malformed optional representation (or "mbcs" missing off Windows)
            # must not hide usable URLs.
            continue
    paths, seen = [], set()
    for candidate in candidates:
        path = _absolute_path(candidate)
        key = ntpath.normcase(path)
        if path and key not in seen:
            seen.add(key)
            paths.append(path)
    return paths


def log_file_drop(event, target, stage, reason, count=0):
    """Record payload format/rejection without logging file names or content."""
    _LOGGER.info(
        "file D&D target=%s stage=%s source=%s count=%s reason=%s "
        "formats=%s proposed=%s possible=%s",
        target, stage, "external" if event.source() is None else "internal",
        count, reason, list(event.mimeData().formats()),
        event.proposedAction().name, event.possibleActions().value,
    )
