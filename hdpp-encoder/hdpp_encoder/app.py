"""hdppエンコーダー本体: STL＋HDPP の書き出し、監視フォルダの追跡、GUI、CLI。

    python -m hdpp_encoder                       # GUI（PyQt6 が必要）
    python -m hdpp_encoder --encode <症例フォルダ> --output <出力先>   # CLI 1回変換

変換ロジックは core.py、設定の保存場所は settings.py、別プロセス連携は ipc.py。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import settings as app_settings
from .core import (
    ConstructionJob,
    EncoderError,
    INVALID_FILENAME_PATTERN,
    append_staff_name,
    build_hdpp_xml,
    clean_path,
    clean_staff_name,
    element_text,
    file_hash,
    find_case_file,
    parse_construction_info,
    safe_filename_stem,
    select_construction_info,
)

APP_NAME = "hdppエンコーダー"
APP_VERSION = "0.4.8"
PACKAGE_DIR = Path(__file__).resolve().parent
# 任意: アイコンを置く場合は hdpp_encoder/assets/ へ（同梱していない）
ASSETS_DIR = PACKAGE_DIR / "assets"
APP_ICON_PATH = ASSETS_DIR / "app_icon.ico"
WINDOW_WIDTH = 760
WINDOW_HEIGHT = 790
ARCHIVE_DIR_NAME = "過去データ"

PAIR_WRITE_LOCK = threading.RLock()


@dataclass
class WrittenPair:
    directory: Path
    stl_path: Path
    hdpp_path: Path
    status: str
    archived_paths: list[Path] = field(default_factory=list)


@dataclass
class CaseResult:
    case_folder: Path
    jobs: list[ConstructionJob] = field(default_factory=list)
    written_pairs: list[WrittenPair] = field(default_factory=list)


def configure_logging() -> Path:
    log_dir = app_settings.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "hdpp_encoder.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )
    return log_path


def normcase_path(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(os.path.normpath(str(path))))


def open_in_file_manager(path: Path) -> None:
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# ---------------------------------------------------------------------------
# STL＋HDPP ペアの書き出し
# ---------------------------------------------------------------------------

def bytes_equal(path: Path, expected: bytes) -> bool:
    try:
        return path.read_bytes() == expected
    except OSError:
        return False


def files_equal(first: Path, second: Path) -> bool:
    try:
        if first.stat().st_size != second.stat().st_size:
            return False
        return file_hash(first, "sha256") == file_hash(second, "sha256")
    except OSError:
        return False


def choose_nonconflicting_pair_paths(
    directory: Path,
    stem: str,
    source_stl: Path,
    hdpp_bytes: bytes,
) -> tuple[Path, Path, str]:
    for index in range(0, 1000):
        suffix = "" if index == 0 else f" ({index})"
        candidate_stem = safe_filename_stem(stem + suffix)
        stl_path = directory / f"{candidate_stem}.stl"
        hdpp_path = directory / f"{candidate_stem}.hdpp"
        if not stl_path.exists() and not hdpp_path.exists():
            return stl_path, hdpp_path, "created"
        if (
            stl_path.is_file()
            and hdpp_path.is_file()
            and files_equal(source_stl, stl_path)
            and bytes_equal(hdpp_path, hdpp_bytes)
        ):
            return stl_path, hdpp_path, "unchanged"
    raise EncoderError(f"同名ファイルの空き番号を確保できません: {directory}")


def pair_matches(
    source_stl: Path,
    hdpp_bytes: bytes,
    stl_path: Path,
    hdpp_path: Path,
) -> bool:
    return (
        stl_path.is_file()
        and hdpp_path.is_file()
        and files_equal(source_stl, stl_path)
        and bytes_equal(hdpp_path, hdpp_bytes)
    )


def archive_existing_pair(stl_path: Path, hdpp_path: Path) -> list[tuple[Path, Path]]:
    existing = [path for path in (stl_path, hdpp_path) if path.exists()]
    if not existing:
        return []

    archive_dir = stl_path.parent / ARCHIVE_DIR_NAME
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_stem = stl_path.stem
    archived_stl: Path | None = None
    archived_hdpp: Path | None = None
    for index in range(0, 1000):
        suffix = "" if index == 0 else f" ({index})"
        candidate_stl = archive_dir / f"{archive_stem}{suffix}.stl"
        candidate_hdpp = archive_dir / f"{archive_stem}{suffix}.hdpp"
        if not candidate_stl.exists() and not candidate_hdpp.exists():
            archived_stl = candidate_stl
            archived_hdpp = candidate_hdpp
            break
    if archived_stl is None or archived_hdpp is None:
        raise EncoderError(f"過去データの空き番号を確保できません: {archive_dir}")

    target_map = {
        stl_path: archived_stl,
        hdpp_path: archived_hdpp,
    }
    moved: list[tuple[Path, Path]] = []
    try:
        for original in existing:
            archived = target_map[original]
            os.replace(original, archived)
            moved.append((original, archived))
    except Exception:
        for original, archived in reversed(moved):
            try:
                if archived.exists() and not original.exists():
                    os.replace(archived, original)
            except OSError:
                logging.exception("過去データ退避のロールバックに失敗しました")
        raise
    return moved


def _write_pair_unlocked(
    job: ConstructionJob,
    case_folder: Path,
    destination: Path,
    archive_existing: bool = False,
    force_replacement: bool = False,
) -> WrittenPair:
    destination.mkdir(parents=True, exist_ok=True)
    hdpp_bytes = build_hdpp_xml(job, case_folder)
    base_stl = destination / f"{job.output_stem}.stl"
    base_hdpp = destination / f"{job.output_stem}.hdpp"
    archived_moves: list[tuple[Path, Path]] = []

    if (
        not force_replacement
        and pair_matches(job.source_stl, hdpp_bytes, base_stl, base_hdpp)
    ):
        stl_path, hdpp_path, status = base_stl, base_hdpp, "unchanged"
    elif not base_stl.exists() and not base_hdpp.exists():
        stl_path, hdpp_path, status = base_stl, base_hdpp, "created"
    elif archive_existing or force_replacement:
        archived_moves = archive_existing_pair(base_stl, base_hdpp)
        stl_path, hdpp_path, status = base_stl, base_hdpp, "replaced"
    else:
        stl_path, hdpp_path, status = choose_nonconflicting_pair_paths(
            destination,
            job.output_stem,
            job.source_stl,
            hdpp_bytes,
        )
    if status == "unchanged":
        return WrittenPair(destination, stl_path, hdpp_path, status)

    # Keep temporary names independent of the full patient/order filename.
    # Repeating that filename plus UUID exceeded MAX_PATH although the final pair fits.
    token = uuid.uuid4().hex
    temp_stl = destination / f".csm-{token}.stl.tmp"
    temp_hdpp = destination / f".csm-{token}.hdpp.tmp"
    stl_committed = False
    try:
        shutil.copy2(job.source_stl, temp_stl)
        with temp_hdpp.open("wb") as handle:
            handle.write(hdpp_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        ET.parse(temp_hdpp)
        if not files_equal(job.source_stl, temp_stl):
            raise EncoderError("出力STLの検証に失敗しました。")
        os.replace(temp_stl, stl_path)
        stl_committed = True
        os.replace(temp_hdpp, hdpp_path)
    except Exception:
        if stl_committed and not hdpp_path.exists():
            try:
                stl_path.unlink()
            except OSError:
                pass
        for original, archived in reversed(archived_moves):
            try:
                if archived.exists() and not original.exists():
                    os.replace(archived, original)
            except OSError:
                logging.exception("旧版ファイルの復元に失敗しました")
        raise
    finally:
        for temporary in (temp_stl, temp_hdpp):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    return WrittenPair(
        destination,
        stl_path,
        hdpp_path,
        status,
        [archived for _, archived in archived_moves],
    )


def write_pair(
    job: ConstructionJob,
    case_folder: Path,
    destination: Path,
    archive_existing: bool = False,
    force_replacement: bool = False,
) -> WrittenPair:
    with PAIR_WRITE_LOCK:
        return _write_pair_unlocked(
            job,
            case_folder,
            destination,
            archive_existing=archive_existing,
            force_replacement=force_replacement,
        )


def encode_case(
    case_folder: Path | str,
    output_path: Path | str,
    also_source: bool = False,
    archive_existing: bool = False,
    force_replacement: bool = False,
    log_callback: Callable[[str], None] | None = None,
    staff_name: str = "",
    material_override: str = "",
) -> CaseResult:
    # Path("") は "." になるため、Path 化する前に未設定を判定する。
    if not app_settings.is_configured_path(output_path):
        raise EncoderError("出力先が設定されていません。設定ファイルまたは --output で指定してください。")
    case_folder = Path(case_folder)
    output_path = Path(clean_path(output_path))
    if not case_folder.is_dir():
        raise EncoderError(f"患者フォルダを確認できません: {case_folder}")

    def report(message: str) -> None:
        logging.info(message)
        if log_callback:
            log_callback(message)

    result = CaseResult(case_folder=case_folder)
    seen_stls: set[str] = set()
    for construction_path in select_construction_info(case_folder):
        report(f"constructionInfo解析: {construction_path.name}")
        for job in parse_construction_info(
            construction_path,
            material_override=material_override,
        ):
            job.output_stem = append_staff_name(job.output_stem, staff_name)
            stl_key = normcase_path(job.source_stl)
            if stl_key in seen_stls:
                continue
            seen_stls.add(stl_key)
            result.jobs.append(job)

    destinations = [output_path]
    if also_source:
        destinations.append(case_folder)
    unique_destinations: list[Path] = []
    seen_destinations: set[str] = set()
    for destination in destinations:
        key = normcase_path(destination)
        if key not in seen_destinations:
            seen_destinations.add(key)
            unique_destinations.append(destination)

    for job in result.jobs:
        report(
            f"変換: {job.source_stl.name} → {job.output_stem}.stl/.hdpp "
            f"[{job.material.hdpp_name}]"
        )
        for destination in unique_destinations:
            pair = write_pair(
                job,
                case_folder,
                destination,
                archive_existing=archive_existing,
                force_replacement=force_replacement,
            )
            result.written_pairs.append(pair)
            action = {
                "unchanged": "既存一致",
                "created": "出力完了",
                "replaced": "旧版退避・新版出力完了",
            }[pair.status]
            report(f"{action}: {pair.stl_path.name} / {destination}")

    if not result.jobs:
        raise EncoderError("変換対象のSTLがありません。")
    return result


# ---------------------------------------------------------------------------
# 監視フォルダの追跡
# ---------------------------------------------------------------------------

def folder_signature(case_folder: Path) -> tuple[tuple[str, int, int], ...] | None:
    """constructionInfo と参照 STL が揃っていればその (名前, サイズ, mtime) 一覧を返す。"""
    try:
        construction_files = [
            path
            for path in case_folder.iterdir()
            if path.is_file() and path.suffix.lower() == ".constructioninfo"
        ]
        if not construction_files:
            return None

        source_stls: dict[str, Path] = {}
        for construction_path in construction_files:
            try:
                root = ET.parse(construction_path).getroot()
            except (ET.ParseError, OSError):
                return None
            constructions = root.findall("./ConstructionFileList/ConstructionFile")
            if not constructions:
                return None
            for construction in constructions:
                filename = element_text(construction, "Filename")
                if not filename:
                    return None
                try:
                    source_stl = find_case_file(case_folder, filename)
                except (EncoderError, OSError):
                    return None
                source_stls[normcase_path(source_stl)] = source_stl
        if not source_stls:
            return None

        files = construction_files + list(source_stls.values())
        return tuple(
            sorted(
                (path.name.lower(), path.stat().st_size, path.stat().st_mtime_ns)
                for path in files
            )
        )
    except OSError:
        return None


class AutoFolderTracker:
    """Tracks only folders that appear after reset(), never the existing baseline."""

    def __init__(self) -> None:
        self.root: Path | None = None
        self.known: set[str] = set()
        self.pending: dict[str, Path] = {}
        self.signatures: dict[str, tuple[tuple[str, int, int], ...]] = {}
        self.stable_counts: dict[str, int] = {}
        self.attempted_signatures: dict[str, tuple[tuple[str, int, int], ...]] = {}

    def reset(self, root: Path | str) -> int:
        self.root = Path(root)
        self.known.clear()
        self.pending.clear()
        self.signatures.clear()
        self.stable_counts.clear()
        self.attempted_signatures.clear()
        try:
            for path in self.root.iterdir():
                if path.is_dir():
                    self.known.add(normcase_path(path))
        except OSError:
            return 0
        return len(self.known)

    def discover(self) -> list[Path]:
        if self.root is None:
            return []
        discovered: list[Path] = []
        try:
            children = [path for path in self.root.iterdir() if path.is_dir()]
        except OSError:
            return discovered
        for path in children:
            key = normcase_path(path)
            if key in self.known:
                continue
            self.known.add(key)
            self.pending[key] = path
            self.stable_counts[key] = 0
            discovered.append(path)
        return discovered

    def ready(self) -> list[Path]:
        ready_paths: list[Path] = []
        for key, path in list(self.pending.items()):
            if not path.is_dir():
                self.pending.pop(key, None)
                self.signatures.pop(key, None)
                self.stable_counts.pop(key, None)
                self.attempted_signatures.pop(key, None)
                continue
            signature = folder_signature(path)
            if signature is None:
                self.stable_counts[key] = 0
                self.signatures.pop(key, None)
                continue
            if signature == self.signatures.get(key):
                self.stable_counts[key] = self.stable_counts.get(key, 0) + 1
            else:
                self.signatures[key] = signature
                self.stable_counts[key] = 0
            if (
                self.stable_counts[key] >= 2
                and signature != self.attempted_signatures.get(key)
            ):
                ready_paths.append(path)
        return ready_paths

    def mark_attempt(self, path: Path | str) -> None:
        key = normcase_path(path)
        signature = self.signatures.get(key)
        if signature is not None:
            self.attempted_signatures[key] = signature

    def is_revision(self, path: Path | str) -> bool:
        return normcase_path(path) in self.attempted_signatures


# ---------------------------------------------------------------------------
# GUI（PyQt6）
# ---------------------------------------------------------------------------

APPLE_STYLE_SHEET = """
    * {
        font-family: "Yu Gothic UI", "Segoe UI Variable", "Segoe UI";
        font-size: 12px;
        color: #1d1d1f;
    }
    QMainWindow, QDialog, QWidget#centralWidget {
        background-color: #f5f5f7;
    }
    QFrame#appHeader {
        background-color: transparent;
        border: none;
        border-bottom: 1px solid #d2d2d7;
    }
    QLabel#appTitle {
        color: #1d1d1f;
        font-size: 20px;
        font-weight: 700;
    }
    QLabel#appSubtitle, QLabel#secondaryLabel {
        color: #6e6e73;
        font-size: 11px;
    }
    QLabel#sectionTitle {
        color: #3a3a3c;
        font-size: 12px;
        font-weight: 700;
    }
    QLabel#liveBadge {
        background-color: #e8f7ee;
        color: #18794e;
        border: 1px solid #b7e4c7;
        border-radius: 8px;
        padding: 2px 8px;
        font-weight: 700;
    }
    QLabel#liveBadge[active="false"] {
        background-color: #f2f2f7;
        color: #6e6e73;
        border-color: #d2d2d7;
    }
    QLabel#countBadge {
        background-color: #e8f2ff;
        color: #0055a4;
        border: 1px solid #b9d8ff;
        border-radius: 8px;
        padding: 2px 8px;
        font-weight: 700;
    }
    QFrame#sectionCard, QFrame#commandBand {
        background-color: #ffffff;
        border: 1px solid #d2d2d7;
        border-radius: 8px;
    }
    QFrame#dropZone {
        background-color: #fbfbfd;
        border: 2px dashed #9bbfe8;
        border-radius: 10px;
    }
    QFrame#dropZone[dragActive="true"] {
        background-color: #e8f2ff;
        border-color: #0071e3;
    }
    QLabel#dropTitle {
        color: #1d1d1f;
        font-size: 18px;
        font-weight: 700;
    }
    QPushButton {
        background-color: #ffffff;
        color: #1d1d1f;
        border: 1px solid #c7c7cc;
        border-radius: 6px;
        min-height: 26px;
        padding: 5px 10px;
    }
    QPushButton:hover {
        background-color: #f2f2f7;
        border-color: #aeaeb2;
    }
    QPushButton:pressed {
        background-color: #e5e5ea;
    }
    QPushButton#primaryButton {
        background-color: #0071e3;
        color: #ffffff;
        border-color: #0071e3;
        font-weight: 700;
    }
    QPushButton#primaryButton:hover {
        background-color: #0077ed;
    }
    QPushButton#toggleButton[active="true"] {
        background-color: #e8f7ee;
        color: #18794e;
        border-color: #86d3a6;
        font-weight: 700;
    }
    QPushButton#toggleButton[active="false"] {
        background-color: #f2f2f7;
        color: #6e6e73;
        border-color: #d2d2d7;
        font-weight: 700;
    }
    QLineEdit {
        background-color: #ffffff;
        border: 1px solid #c7c7cc;
        border-radius: 6px;
        padding: 6px 8px;
        selection-background-color: #0071e3;
        selection-color: #ffffff;
    }
    QLineEdit:focus {
        border: 2px solid #0071e3;
        padding: 5px 7px;
    }
    QListWidget, QTextEdit {
        background-color: #ffffff;
        alternate-background-color: #f7f7f9;
        color: #1d1d1f;
        border: 1px solid #c7c7cc;
        border-radius: 8px;
        outline: none;
        selection-background-color: #dbeeff;
        selection-color: #1d1d1f;
    }
    QListWidget::item {
        border: none;
        padding: 5px 7px;
    }
    QScrollBar:vertical {
        background: transparent;
        width: 10px;
        margin: 2px;
    }
    QScrollBar::handle:vertical {
        background: #c7c7cc;
        min-height: 28px;
        border-radius: 5px;
    }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
        height: 0px;
    }
"""


try:
    from PyQt6.QtCore import QObject, QLockFile, QSize, QTimer, Qt, pyqtSignal
    from PyQt6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent, QIcon
    from PyQt6.QtWidgets import (
        QApplication,
        QDialog,
        QFileDialog,
        QFrame,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QTextEdit,
        QVBoxLayout,
        QWidget,
    )

    PYQT_AVAILABLE = True
    PYQT_IMPORT_ERROR: Exception | None = None
except Exception as error:  # PyQt6 なしでも CLI / IPC は使える
    PYQT_AVAILABLE = False
    PYQT_IMPORT_ERROR = error


if PYQT_AVAILABLE:

    def set_button_icon(button: QPushButton, file_name: str, size: int = 18) -> None:
        """assets/ にアイコンがあれば付ける（無ければ何もしない）。"""
        path = ASSETS_DIR / file_name
        if path.is_file():
            button.setIcon(QIcon(str(path)))
            button.setIconSize(QSize(size, size))

    def fit_window_to_primary(
        window: QWidget,
        preferred_width: int,
        preferred_height: int,
        minimum_width: int = 760,
        minimum_height: int = 620,
    ) -> None:
        screen = window.screen() or QApplication.primaryScreen()
        if screen is None:
            window.resize(preferred_width, preferred_height)
            return
        available = screen.availableGeometry()
        width = min(preferred_width, max(320, available.width() - 48))
        height = min(preferred_height, max(320, available.height() - 48))
        window.setMinimumSize(min(minimum_width, width), min(minimum_height, height))
        window.resize(width, height)
        window.move(
            available.x() + (available.width() - width) // 2,
            available.y() + (available.height() - height) // 2,
        )

    class DropZone(QFrame):
        folders_dropped = pyqtSignal(list)

        def __init__(self) -> None:
            super().__init__()
            self.setObjectName("dropZone")
            self.setProperty("dragActive", False)
            self.setAcceptDrops(True)
            self.setMinimumHeight(170)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(24, 22, 24, 22)
            layout.setSpacing(7)
            layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            title = QLabel("患者フォルダをここへドロップ")
            title.setObjectName("dropTitle")
            title.setAlignment(Qt.AlignmentFlag.AlignCenter)
            description = QLabel(
                "STLとconstructionInfoを検出し、同名のSTL＋HDPPセットを作成します。"
            )
            description.setObjectName("secondaryLabel")
            description.setAlignment(Qt.AlignmentFlag.AlignCenter)
            description.setWordWrap(True)
            layout.addWidget(title)
            layout.addWidget(description)

        def set_drag_active(self, active: bool) -> None:
            self.setProperty("dragActive", active)
            self.style().unpolish(self)
            self.style().polish(self)

        def dragEnterEvent(self, event: QDragEnterEvent) -> None:
            urls = event.mimeData().urls()
            if any(Path(url.toLocalFile()).is_dir() for url in urls):
                self.set_drag_active(True)
                event.acceptProposedAction()
            else:
                event.ignore()

        def dragLeaveEvent(self, event) -> None:
            self.set_drag_active(False)
            super().dragLeaveEvent(event)

        def dropEvent(self, event: QDropEvent) -> None:
            self.set_drag_active(False)
            folders = [
                Path(url.toLocalFile())
                for url in event.mimeData().urls()
                if Path(url.toLocalFile()).is_dir()
            ]
            if folders:
                self.folders_dropped.emit(folders)
                event.acceptProposedAction()
            else:
                event.ignore()

    class WorkerBridge(QObject):
        log_message = pyqtSignal(str)
        finished = pyqtSignal(str, bool, str)

    class PathSettingsDialog(QDialog):
        def __init__(self, parent, settings: dict[str, object]) -> None:
            super().__init__(parent)
            self.setWindowTitle("出力・監視・作業者設定")
            self.setWindowIcon(parent.windowIcon())
            self.setMinimumWidth(760)
            self.values: dict[str, str] | None = None

            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(14)
            description = QLabel(
                "出力先・自動監視先と、生成ファイル名の末尾へ付ける作業者名を指定します。"
            )
            description.setObjectName("secondaryLabel")
            layout.addWidget(description)

            grid = QGridLayout()
            grid.setHorizontalSpacing(8)
            grid.setVerticalSpacing(9)
            self.output_edit = QLineEdit(str(settings.get("output_path", "")))
            self.output_edit.setPlaceholderText(app_settings.PLACEHOLDER_OUTPUT_PATH)
            self.watch_edit = QLineEdit(str(settings.get("watch_path", "")))
            self.watch_edit.setPlaceholderText(app_settings.PLACEHOLDER_WATCH_PATH)
            self.staff_name_edit = QLineEdit(str(settings.get("staff_name", "")))
            self.staff_name_edit.setMaxLength(40)
            self.staff_name_edit.setPlaceholderText("例: user01")
            self.staff_name_edit.setToolTip(
                "生成するSTL・HDPPのファイル名末尾へ「_作業者名」として付与します（空欄なら付与しません）"
            )
            output_button = QPushButton("参照")
            watch_button = QPushButton("参照")
            set_button_icon(output_button, "folder.png")
            set_button_icon(watch_button, "folder.png")
            output_button.clicked.connect(
                lambda: self.browse(self.output_edit, "出力先を選択")
            )
            watch_button.clicked.connect(
                lambda: self.browse(self.watch_edit, "監視先を選択")
            )
            grid.addWidget(QLabel("出力先"), 0, 0)
            grid.addWidget(self.output_edit, 0, 1)
            grid.addWidget(output_button, 0, 2)
            grid.addWidget(QLabel("監視先"), 1, 0)
            grid.addWidget(self.watch_edit, 1, 1)
            grid.addWidget(watch_button, 1, 2)
            grid.addWidget(QLabel("作業者名"), 2, 0)
            grid.addWidget(self.staff_name_edit, 2, 1)
            grid.setColumnStretch(1, 1)
            layout.addLayout(grid)

            note = QLabel(
                "自動変換ON中に監視先を変更した場合、新しい監視先の既存フォルダは"
                "すべて基準登録され、処理されません。"
            )
            note.setObjectName("secondaryLabel")
            note.setWordWrap(True)
            layout.addWidget(note)

            buttons = QHBoxLayout()
            buttons.addStretch()
            cancel_button = QPushButton("キャンセル")
            save_button = QPushButton("保存")
            save_button.setObjectName("primaryButton")
            set_button_icon(save_button, "save_icon.png")
            cancel_button.clicked.connect(self.reject)
            save_button.clicked.connect(self.save)
            buttons.addWidget(cancel_button)
            buttons.addWidget(save_button)
            layout.addLayout(buttons)

        def browse(self, edit: QLineEdit, title: str) -> None:
            current = clean_path(edit.text())
            start = current if os.path.isdir(current) else os.path.dirname(current)
            if not start or not os.path.isdir(start):
                start = str(Path.home())
            selected = QFileDialog.getExistingDirectory(self, title, start)
            if selected:
                edit.setText(os.path.normpath(selected))

        def save(self) -> None:
            output_path = clean_path(self.output_edit.text())
            watch_path = clean_path(self.watch_edit.text())
            raw_staff_name = self.staff_name_edit.text().strip()
            staff_name = clean_staff_name(raw_staff_name)
            if not app_settings.is_configured_path(output_path):
                QMessageBox.warning(self, "未入力", "出力先を入力してください。")
                return
            if INVALID_FILENAME_PATTERN.search(raw_staff_name):
                QMessageBox.warning(
                    self,
                    "使用できない作業者名",
                    "作業者名にファイル名で使用できない記号が含まれています。",
                )
                return
            self.values = {
                "output_path": os.path.normpath(output_path),
                "watch_path": os.path.normpath(watch_path) if watch_path else "",
                "staff_name": staff_name,
            }
            self.accept()

    class MainWindow(QMainWindow):
        def __init__(self, settings: dict[str, object]) -> None:
            super().__init__()
            self.settings = settings
            self.setWindowTitle(APP_NAME)
            fit_window_to_primary(
                self,
                WINDOW_WIDTH,
                WINDOW_HEIGHT,
                minimum_width=680,
                minimum_height=640,
            )
            self.setFixedSize(self.size())
            self.setStyleSheet(APPLE_STYLE_SHEET)
            if APP_ICON_PATH.exists():
                self.setWindowIcon(QIcon(str(APP_ICON_PATH)))

            self.bridge = WorkerBridge()
            self.bridge.log_message.connect(self.append_log)
            self.bridge.finished.connect(self.worker_finished)
            self.active_workers: set[str] = set()
            self.worker_threads: set[threading.Thread] = set()
            self.tracker = AutoFolderTracker()
            self.monitor_error_last = 0.0

            self.setup_ui()
            self.monitor_timer = QTimer(self)
            self.monitor_timer.setInterval(2000)
            self.monitor_timer.timeout.connect(self.monitor_tick)

            if bool(self.settings.get("auto_enabled")):
                self.enable_auto(initial=True)
            else:
                self.update_auto_ui(False)
            self.refresh_paths()
            if not app_settings.is_configured_path(self.settings.get("output_path")):
                self.log("出力先が未設定です。「出力・監視・作業者設定」で指定してください。")

        def setup_ui(self) -> None:
            central = QWidget()
            central.setObjectName("centralWidget")
            layout = QVBoxLayout(central)
            layout.setContentsMargins(16, 14, 16, 16)
            layout.setSpacing(12)
            self.setCentralWidget(central)

            header = QFrame()
            header.setObjectName("appHeader")
            header.setFixedHeight(58)
            header_layout = QHBoxLayout(header)
            header_layout.setContentsMargins(2, 0, 2, 10)
            title_layout = QVBoxLayout()
            title_layout.setSpacing(1)
            title = QLabel(APP_NAME)
            title.setObjectName("appTitle")
            subtitle = QLabel("exocad constructionInfo → DWOS互換HDPP")
            subtitle.setObjectName("appSubtitle")
            title_layout.addWidget(title)
            title_layout.addWidget(subtitle)
            header_layout.addLayout(title_layout)
            header_layout.addStretch()
            self.auto_badge = QLabel("自動変換 OFF")
            self.auto_badge.setObjectName("liveBadge")
            self.auto_badge.setProperty("active", False)
            header_layout.addWidget(self.auto_badge)
            layout.addWidget(header)

            self.drop_zone = DropZone()
            self.drop_zone.folders_dropped.connect(self.manual_folders)
            layout.addWidget(self.drop_zone)

            browse_row = QHBoxLayout()
            browse_row.addStretch()
            browse_button = QPushButton("フォルダを選択して変換")
            browse_button.setObjectName("primaryButton")
            set_button_icon(browse_button, "folder.png")
            browse_button.clicked.connect(self.browse_case)
            browse_row.addWidget(browse_button)
            browse_row.addStretch()
            layout.addLayout(browse_row)

            command = QFrame()
            command.setObjectName("commandBand")
            command_layout = QVBoxLayout(command)
            command_layout.setContentsMargins(12, 10, 12, 10)
            command_layout.setSpacing(8)

            self.output_label = QLabel()
            self.output_label.setObjectName("secondaryLabel")
            self.output_label.setWordWrap(True)
            self.watch_label = QLabel()
            self.watch_label.setObjectName("secondaryLabel")
            self.watch_label.setWordWrap(True)
            self.staff_label = QLabel()
            self.staff_label.setObjectName("secondaryLabel")
            command_layout.addWidget(self.output_label)
            command_layout.addWidget(self.watch_label)
            command_layout.addWidget(self.staff_label)

            action_row = QHBoxLayout()
            self.auto_button = QPushButton("自動エンコード / OFF")
            self.auto_button.setObjectName("toggleButton")
            self.auto_button.setProperty("active", False)
            self.auto_button.clicked.connect(self.toggle_auto)
            path_button = QPushButton("出力・監視・作業者設定")
            set_button_icon(path_button, "folder.png")
            path_button.clicked.connect(self.open_path_settings)
            open_button = QPushButton("出力先を開く")
            set_button_icon(open_button, "folder.png")
            open_button.clicked.connect(self.open_output_folder)
            action_row.addWidget(self.auto_button)
            action_row.addWidget(path_button)
            action_row.addWidget(open_button)
            command_layout.addLayout(action_row)
            layout.addWidget(command)

            recent_header = QHBoxLayout()
            recent_title = QLabel("処理結果")
            recent_title.setObjectName("sectionTitle")
            self.result_count = QLabel("0 件")
            self.result_count.setObjectName("countBadge")
            recent_header.addWidget(recent_title)
            recent_header.addStretch()
            recent_header.addWidget(self.result_count)
            layout.addLayout(recent_header)
            self.results = QListWidget()
            self.results.setAlternatingRowColors(True)
            self.results.setFixedHeight(115)
            layout.addWidget(self.results)

            log_title = QLabel("処理ログ")
            log_title.setObjectName("sectionTitle")
            layout.addWidget(log_title)
            self.log_text = QTextEdit()
            self.log_text.setReadOnly(True)
            layout.addWidget(self.log_text, 1)

        def append_log(self, message: str) -> None:
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
            self.log_text.append(f"{timestamp} - {message}")
            self.log_text.ensureCursorVisible()

        def log(self, message: str) -> None:
            self.append_log(message)
            logging.info(message)

        def add_result(self, message: str) -> None:
            self.results.insertItem(0, message)
            while self.results.count() > 30:
                self.results.takeItem(self.results.count() - 1)
            self.result_count.setText(f"{self.results.count()} 件")

        def refresh_paths(self) -> None:
            output = self.settings.get("output_path") or "（未設定）"
            watch = self.settings.get("watch_path") or "（未設定）"
            staff = self.settings.get("staff_name") or ""
            self.output_label.setText(f"出力先: {output}")
            self.watch_label.setText(f"監視先: {watch}")
            self.staff_label.setText(f"作業者名: _{staff}" if staff else "作業者名: （付与しない）")

        def browse_case(self) -> None:
            selected = QFileDialog.getExistingDirectory(
                self,
                "患者フォルダを選択",
                str(Path.home()),
            )
            if selected:
                self.manual_folders([Path(selected)])

        def manual_folders(self, folders: list[Path]) -> None:
            for folder in folders:
                self.start_worker(folder, also_source=False, automatic=False)

        def start_worker(
            self,
            folder: Path,
            also_source: bool,
            automatic: bool,
        ) -> None:
            key = normcase_path(folder)
            if key in self.active_workers:
                self.log(f"処理中のため重複開始を省略: {folder.name}")
                return
            is_revision = automatic and self.tracker.is_revision(folder)
            if automatic:
                self.tracker.mark_attempt(folder)
            self.active_workers.add(key)
            mode = "自動" if automatic else "手動"
            self.log(f"{mode}変換開始: {folder}")

            def worker() -> None:
                success = False
                summary = ""
                try:
                    result = encode_case(
                        folder,
                        str(self.settings.get("output_path", "")),
                        also_source=also_source,
                        archive_existing=automatic,
                        force_replacement=is_revision,
                        log_callback=self.bridge.log_message.emit,
                        staff_name=str(self.settings.get("staff_name", "")),
                    )
                    created = sum(
                        pair.status == "created" for pair in result.written_pairs
                    )
                    unchanged = sum(
                        pair.status == "unchanged" for pair in result.written_pairs
                    )
                    replaced = sum(
                        pair.status == "replaced" for pair in result.written_pairs
                    )
                    summary = (
                        f"{folder.name}: {len(result.jobs)}セット "
                        f"(新規{created} / 更新{replaced} / 既存一致{unchanged})"
                    )
                    success = True
                except Exception as error:
                    logging.exception("変換に失敗しました")
                    summary = f"{folder.name}: {error}"
                finally:
                    self.bridge.finished.emit(key, success, summary)
                    self.worker_threads.discard(threading.current_thread())

            thread = threading.Thread(
                target=worker,
                name=f"hdpp-encode-{folder.name}",
                daemon=True,
            )
            self.worker_threads.add(thread)
            thread.start()

        def worker_finished(self, key: str, success: bool, summary: str) -> None:
            self.active_workers.discard(key)
            if success:
                self.add_result("完了  " + summary)
                self.log("変換完了: " + summary)
            else:
                self.add_result("エラー  " + summary)
                self.log("変換失敗: " + summary)

        def toggle_auto(self) -> None:
            if bool(self.settings.get("auto_enabled")):
                self.disable_auto()
            else:
                self.enable_auto(initial=False)

        def enable_auto(self, initial: bool) -> None:
            watch_text = clean_path(self.settings.get("watch_path"))
            if not app_settings.is_configured_path(watch_text) or not app_settings.is_configured_path(
                self.settings.get("output_path")
            ):
                # Path("") はカレントディレクトリになるため、未設定では監視しない。
                self.settings["auto_enabled"] = False
                self.update_auto_ui(False)
                self.log("自動変換を開始できません: 出力先と監視先を設定してください。")
                return
            watch_path = Path(watch_text)
            baseline_count = self.tracker.reset(watch_path)
            self.settings["auto_enabled"] = True
            app_settings.save_settings(self.settings)
            self.monitor_timer.start()
            self.update_auto_ui(True)
            action = "起動時再開" if initial else "ON"
            if watch_path.is_dir():
                self.log(
                    f"自動変換{action}: 既存{baseline_count}フォルダを基準登録。"
                    "今後作成される新規フォルダだけを処理します。"
                )
            else:
                self.log(
                    f"自動変換{action}: 監視先に接続できません。復旧後も、"
                    f"接続時点の既存フォルダは処理対象外になります: {watch_path}"
                )

        def disable_auto(self) -> None:
            self.monitor_timer.stop()
            self.settings["auto_enabled"] = False
            app_settings.save_settings(self.settings)
            self.update_auto_ui(False)
            self.tracker.pending.clear()
            self.tracker.signatures.clear()
            self.tracker.stable_counts.clear()
            self.tracker.attempted_signatures.clear()
            self.log("自動変換をOFFにしました。")

        def update_auto_ui(self, active: bool) -> None:
            self.auto_button.setText(
                "自動エンコード / ON" if active else "自動エンコード / OFF"
            )
            self.auto_button.setProperty("active", active)
            self.auto_badge.setText("自動変換 ON" if active else "自動変換 OFF")
            self.auto_badge.setProperty("active", active)
            set_button_icon(self.auto_button, "iconON.png" if active else "iconOFF.png")
            for widget in (self.auto_button, self.auto_badge):
                widget.style().unpolish(widget)
                widget.style().polish(widget)

        def monitor_tick(self) -> None:
            watch_text = clean_path(self.settings.get("watch_path"))
            if not app_settings.is_configured_path(watch_text):
                return
            watch_path = Path(watch_text)
            if self.tracker.root is None or normcase_path(self.tracker.root) != normcase_path(watch_path):
                baseline_count = self.tracker.reset(watch_path)
                self.log(
                    f"監視先を再基準登録しました。既存{baseline_count}フォルダは処理しません。"
                )
            if not watch_path.is_dir():
                now = time.monotonic()
                if now - self.monitor_error_last >= 30:
                    self.log(f"監視先に接続できません: {watch_path}")
                    self.monitor_error_last = now
                return
            for path in self.tracker.discover():
                self.log(f"新規患者フォルダを検出: {path.name}")
            for path in self.tracker.ready():
                key = normcase_path(path)
                if key not in self.active_workers:
                    if self.tracker.is_revision(path):
                        self.log(
                            f"元データ更新を検出: {path.name} / "
                            "旧版を過去データへ退避して再生成します。"
                        )
                    self.start_worker(path, also_source=True, automatic=True)

        def open_path_settings(self) -> None:
            dialog = PathSettingsDialog(self, self.settings)
            if dialog.exec() != QDialog.DialogCode.Accepted or dialog.values is None:
                return
            old_watch = normcase_path(str(self.settings.get("watch_path", "")))
            self.settings.update(dialog.values)
            try:
                app_settings.save_settings(self.settings)
            except OSError as error:
                QMessageBox.critical(self, "保存エラー", f"設定を保存できませんでした。\n{error}")
                self.log(f"設定の保存に失敗しました: {error}")
                return
            self.refresh_paths()
            if bool(self.settings.get("auto_enabled")):
                if not app_settings.is_configured_path(self.settings.get("watch_path")):
                    self.disable_auto()
                elif old_watch != normcase_path(str(self.settings["watch_path"])):
                    baseline_count = self.tracker.reset(str(self.settings["watch_path"]))
                    self.log(
                        f"監視先変更: 既存{baseline_count}フォルダを基準登録しました。"
                    )
            self.log("出力・監視・作業者設定を保存しました。")

        def open_output_folder(self) -> None:
            output_text = clean_path(self.settings.get("output_path"))
            if not app_settings.is_configured_path(output_text):
                QMessageBox.information(self, APP_NAME, "出力先が未設定です。")
                return
            output_path = Path(output_text)
            try:
                output_path.mkdir(parents=True, exist_ok=True)
                open_in_file_manager(output_path)
            except Exception as error:
                QMessageBox.warning(
                    self,
                    "フォルダを開けません",
                    f"{output_path}\n\n{error}",
                )

        def closeEvent(self, event: QCloseEvent) -> None:
            if self.active_workers:
                QMessageBox.information(
                    self,
                    "処理中です",
                    "変換処理が完了してからウィンドウを閉じてください。",
                )
                event.ignore()
                return
            self.monitor_timer.stop()
            app_settings.save_settings(self.settings)
            event.accept()


def run_gui() -> int:
    if not PYQT_AVAILABLE:
        message = (
            "PyQt6を読み込めません。`pip install PyQt6` を実行してください。\n"
            f"詳細: {PYQT_IMPORT_ERROR}"
        )
        print(message, file=sys.stderr)
        return 2

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    if APP_ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    lock_path = str(Path(tempfile.gettempdir()) / "hdpp_encoder_gui.lock")
    app_lock = QLockFile(lock_path)
    app_lock.setStaleLockTime(0)
    if not app_lock.tryLock(100):
        QMessageBox.information(None, APP_NAME, "hdppエンコーダーは既に起動しています。")
        return 0

    settings = app_settings.load_settings()
    window = MainWindow(settings)
    window.show()
    if os.environ.get("HDPP_ENCODER_SMOKE_TEST") == "1":
        QTimer.singleShot(750, app.quit)
    result = app.exec()
    app_lock.unlock()
    return result


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hdpp_encoder", description=APP_NAME)
    parser.add_argument(
        "--encode",
        metavar="PATIENT_FOLDER",
        help="GUIを開かず、指定患者フォルダを1回変換します。",
    )
    parser.add_argument(
        "--output",
        metavar="OUTPUT_FOLDER",
        help="CLI変換時の出力先。省略時は保存設定を使用します。",
    )
    parser.add_argument(
        "--staff-name",
        metavar="NAME",
        help="CLI変換時、ファイル名末尾に付ける作業者名。省略時は保存設定を使用します。",
    )
    parser.add_argument(
        "--material",
        metavar="GROUP",
        help="constructionInfoに材料がない場合に使う材料（ZR/CC/TI/WAX/TEMP/PEEK）。",
    )
    parser.add_argument(
        "--also-source",
        action="store_true",
        help="CLI変換時、患者フォルダ内にもSTL＋HDPPを出力します。",
    )
    parser.add_argument(
        "--archive-existing",
        action="store_true",
        help="CLI変換時、内容が異なる既存セットを過去データへ退避します。",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{APP_NAME} {APP_VERSION}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    log_path = configure_logging()
    logging.info(
        "%s %s を起動しました / settings=%s / log=%s",
        APP_NAME,
        APP_VERSION,
        app_settings.config_path(),
        log_path,
    )
    parser = build_argument_parser()
    arguments = parser.parse_args(argv)
    if arguments.encode:
        settings = app_settings.load_settings()
        output = arguments.output or str(settings.get("output_path", ""))
        staff_name = (
            arguments.staff_name
            if arguments.staff_name is not None
            else str(settings.get("staff_name", ""))
        )
        try:
            result = encode_case(
                Path(arguments.encode),
                output,
                also_source=arguments.also_source,
                archive_existing=arguments.archive_existing,
                staff_name=staff_name,
                material_override=arguments.material or "",
            )
        except Exception as error:
            logging.exception("CLI変換に失敗しました")
            print(f"エラー: {error}", file=sys.stderr)
            return 1
        summary = {
            "case_folder": str(result.case_folder),
            "jobs": [
                {
                    "source_stl": str(job.source_stl),
                    "output_stem": job.output_stem,
                    "material": job.material.hdpp_name,
                    "tooth_numbers": job.tooth_numbers,
                    "margin_counts": [
                        count
                        for margin in job.margins
                        for count in [len(margin.points)]
                        + [len(loop.points) for loop in margin.additional_margins]
                    ],
                }
                for job in result.jobs
            ],
            "written_pairs": [
                {
                    "stl": str(pair.stl_path),
                    "hdpp": str(pair.hdpp_path),
                    "status": pair.status,
                    "archived": [str(path) for path in pair.archived_paths],
                }
                for pair in result.written_pairs
            ],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    return run_gui()
