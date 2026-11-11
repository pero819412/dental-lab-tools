"""設計ファイルの移動キュー: リネーム済みの STL/HDPP を、別プロセスの移動ワーカーへ安全に受け渡す。

痒いところ: リネーム直後にすぐ移動すると、六面図（スクリーンショット）作成など後続処理が
まだファイルを読んでいる途中だったりする。依頼を1件1ファイルの原子的な書き込みでキューに積み、
「六面図が終わるまで待つ」保留や、claim/ack・指数バックオフ付きリトライで取りこぼしなく処理する。

キューの置き場所: set_queue_root() で指定 → 環境変数 DESIGN_MOVE_QUEUE_ROOT →
このファイルと同じフォルダの queue_data/。標準ライブラリのみ。
"""
import os
import time
import uuid
import json
from pathlib import Path


REQUEST_SUFFIX = ".request"
WAITING_SUFFIX = ".waiting"
PROCESSING_SUFFIX = ".processing"
SUPPORTED_EXTENSIONS = frozenset({".stl", ".hdpp", ".wncdw"})

_queue_root = None


def set_queue_root(path):
    """キューの置き場所を設定する（プロデューサーとワーカーで同じ場所にすること）。"""
    global _queue_root
    _queue_root = Path(path) if path else None


def queue_root():
    if _queue_root is not None:
        return _queue_root
    explicit = os.environ.get("DESIGN_MOVE_QUEUE_ROOT", "").strip()
    if explicit:
        return Path(explicit)
    return Path(__file__).resolve().parent / "queue_data"


def _queue_dir():
    return queue_root() / "design_file_move_queue"


def _after_six_view_dir():
    return queue_root() / "design_file_move_after_six_view"


def _normalized_unique_paths(paths):
    normalized = []
    known = set()
    for path in paths or ():
        path_text = str(path or "").strip()
        if not path_text:
            continue
        normalized_path = os.path.normpath(path_text)
        path_key = os.path.normcase(os.path.abspath(normalized_path))
        if path_key in known:
            continue
        known.add(path_key)
        normalized.append(normalized_path)
    return normalized


def _path_base_key(path):
    return os.path.normcase(os.path.abspath(os.path.splitext(path)[0]))


def _atomic_write_lines(directory, suffix, lines):
    directory.mkdir(parents=True, exist_ok=True)
    request_id = f"{time.time_ns()}_{os.getpid()}_{uuid.uuid4().hex}"
    temporary_path = directory / f"{request_id}.tmp"
    request_path = directory / f"{request_id}{suffix}"
    try:
        temporary_path.write_text(
            "\n".join(lines),
            encoding="utf-8-sig",
        )
        os.replace(temporary_path, request_path)
    finally:
        try:
            temporary_path.unlink()
        except OSError:
            pass
    return request_path


def enqueue_design_move_paths(paths):
    targets = [
        path
        for path in _normalized_unique_paths(paths)
        if os.path.splitext(path)[1].casefold() in SUPPORTED_EXTENSIONS
    ]
    created = []
    try:
        for path in targets:
            created.append(
                _atomic_write_lines(_queue_dir(), REQUEST_SUFFIX, [path])
            )
    except OSError:
        for created_path in created:
            try:
                created_path.unlink()
            except OSError:
                pass
        raise
    return len(created)


def defer_design_move_until_six_view(source_path, paths):
    source_path = os.path.normpath(str(source_path or "").strip())
    targets = [
        path
        for path in _normalized_unique_paths(paths)
        if os.path.splitext(path)[1].casefold() in SUPPORTED_EXTENSIONS
    ]
    if not source_path or not targets:
        return False
    _atomic_write_lines(
        _after_six_view_dir(),
        WAITING_SUFFIX,
        [source_path, *targets],
    )
    return True


def queue_renamed_design_paths(paths, six_view_paths=(), on_record=None):
    """リネーム済みパスをキューへ積む。six_view_paths の STL と同名のものは六面図完了まで保留。

    on_record: 任意のコールバック。キュー投入前に paths を渡して呼ぶ
               （元実装では症例アーカイブへの記録に使っていた）。
    """
    if on_record is not None:
        on_record(paths)
    targets = [
        path
        for path in _normalized_unique_paths(paths)
        if os.path.splitext(path)[1].casefold() in SUPPORTED_EXTENSIONS
    ]
    six_view_targets = [
        path
        for path in _normalized_unique_paths(six_view_paths)
        if os.path.splitext(path)[1].casefold() == ".stl"
    ]
    deferred_base_keys = {_path_base_key(path) for path in six_view_targets}

    deferred_count = 0
    for source_path in six_view_targets:
        source_base_key = _path_base_key(source_path)
        related_paths = [
            path for path in targets if _path_base_key(path) == source_base_key
        ]
        if defer_design_move_until_six_view(source_path, related_paths):
            deferred_count += len(related_paths)

    immediate_paths = [
        path for path in targets if _path_base_key(path) not in deferred_base_keys
    ]
    queued_count = enqueue_design_move_paths(immediate_paths)
    return {
        "queued": queued_count,
        "deferred": deferred_count,
        "ignored": max(0, len(_normalized_unique_paths(paths)) - len(targets)),
    }


def release_design_move_after_six_view(source_path):
    source_text = str(source_path or "").strip()
    if not source_text:
        return 0
    source_key = os.path.normcase(
        os.path.abspath(os.path.normpath(source_text))
    )
    after_dir = _after_six_view_dir()
    after_dir.mkdir(parents=True, exist_ok=True)
    released_count = 0
    waiting_paths = sorted(
        after_dir.glob(f"*{WAITING_SUFFIX}"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
    )
    for waiting_path in waiting_paths:
        processing_path = waiting_path.with_suffix(PROCESSING_SUFFIX)
        try:
            os.replace(waiting_path, processing_path)
            lines = [
                line.strip()
                for line in processing_path.read_text(
                    encoding="utf-8-sig"
                ).splitlines()
                if line.strip()
            ]
            waiting_source_key = (
                os.path.normcase(os.path.abspath(os.path.normpath(lines[0])))
                if lines
                else ""
            )
            if waiting_source_key != source_key:
                os.replace(processing_path, waiting_path)
                continue
            released_count += enqueue_design_move_paths(lines[1:])
            processing_path.unlink()
        except FileNotFoundError:
            continue
        except OSError:
            try:
                if processing_path.exists() and not waiting_path.exists():
                    os.replace(processing_path, waiting_path)
            except OSError:
                pass
            raise
    return released_count


def pop_next_design_move_path():
    queue_dir = _queue_dir()
    queue_dir.mkdir(parents=True, exist_ok=True)
    requests = sorted(
        queue_dir.glob(f"*{REQUEST_SUFFIX}"),
        key=lambda path: (path.stat().st_mtime_ns, path.name),
    )
    for request_path in requests:
        processing_path = request_path.with_suffix(PROCESSING_SUFFIX)
        try:
            os.replace(request_path, processing_path)
            try:
                source_path = processing_path.read_text(
                    encoding="utf-8-sig"
                ).strip()
            finally:
                try:
                    processing_path.unlink()
                except OSError:
                    pass
            if source_path:
                return os.path.normpath(source_path)
        except (FileNotFoundError, OSError):
            continue
    return None


# The worker uses claim/ack. The legacy pop API remains for older callers;
# producers and the six-view release format remain compatible.
def _write_claim_payload(path, payload):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def recover_design_move_claims():
    """Call only while holding the single worker lock, before processing jobs."""
    queue_dir = _queue_dir()
    queue_dir.mkdir(parents=True, exist_ok=True)
    for path in queue_dir.glob('*' + PROCESSING_SUFFIX):
        target = path.with_suffix(REQUEST_SUFFIX)
        if target.exists():
            raise RuntimeError('同じ移動要求の復旧先が既にあります')
        os.rename(path, target)


def claim_next_design_move_request():
    queue_dir = _queue_dir()
    queue_dir.mkdir(parents=True, exist_ok=True)
    candidates = []
    for suffix in (REQUEST_SUFFIX, '.retry'):
        for path in queue_dir.glob('*' + suffix):
            try:
                candidates.append((path.stat().st_mtime_ns, path.name, path))
            except FileNotFoundError:
                continue
    for _, _, path in sorted(candidates):
        try:
            text = path.read_text(encoding='utf-8-sig').strip()
            payload = json.loads(text) if text.startswith('{') else {'path': text, 'phase': 'design', 'attempt': 0}
            if (not isinstance(payload.get('path'), str) or not payload['path'].strip() or
                payload.get('phase', 'design') not in {'design', 'models'}):
                raise ValueError('Invalid move request')
            if float(payload.get('next_attempt_at', 0)) > time.time():
                continue
            processing = path.with_suffix(PROCESSING_SUFFIX)
            if processing.exists():
                continue
            os.rename(path, processing)
            return {'file': processing, 'payload': payload}
        except FileNotFoundError:
            continue
        except (ValueError, TypeError):
            os.rename(path, path.with_suffix('.blocked'))
    return None


def advance_design_move_request(request, phase, **fields):
    request['payload'].update(phase=phase, attempt=0, next_attempt_at=0, **fields)
    _write_claim_payload(request['file'], request['payload'])


def finish_design_move_request(request, error=None, retryable=True, delay=3):
    path = request['file']
    if error is None:
        path.unlink()
        return 0
    payload = request['payload']
    attempt = int(payload.get('attempt', 0)) + 1
    seconds = min(60.0, max(1.0, float(delay)) * 2 ** min(attempt - 1, 5))
    payload.update(attempt=attempt, last_error=str(error), updated_at=time.time(),
                   next_attempt_at=time.time() + seconds if retryable else 0)
    _write_claim_payload(path, payload)
    target = path.with_suffix('.retry' if retryable else '.blocked')
    if target.exists():
        raise RuntimeError('移動要求の保存先が重複しました')
    os.rename(path, target)
    return seconds if retryable else 0
