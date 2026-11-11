"""移動トランザクション（Windows 専用）: STL と HDPP のペアを「両方そろって移動」か「何もしない」にする。

痒いところ: 共有フォルダへのペア移動が途中で落ちると、片方だけ移動・片方だけ残る・同名の別ファイルを
上書き、といった事故が起きる。元ファイルを DELETE 権限付きで確保 → 一時名へコピー＆SHA-256 検証 →
公開（rename、上書きしない）→ 最後に元を削除、をジャーナル付きで行い、中断後の再実行でも安全に再開／巻き戻す。

Windows group transfer: claim sources, stage/verify, publish, then delete sources.
Final names are never copy targets. Journals distinguish our own interrupted work
from unrelated files with the same name. Unknown/changed files fail closed.

依存: 標準ライブラリのみだが、msvcrt と kernel32（CreateFileW / SetFileInformationByHandle）を
import 時に使うため Windows でしか import できない（他 OS では構文チェックのみ可）。
"""
import contextlib
import ctypes
from ctypes import wintypes
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import time
import uuid

VERSION = '20261104.paired-move.1'
# 一時コピーのファイル名接頭辞（移動先フォルダに一時的に作られる隠しファイル）
STAGE_PREFIX = '.csm-moving-'


class TransferFailure(Exception):
    def __init__(self, message, retryable=True):
        super().__init__(message)
        self.retryable = retryable


def _identity(stat):
    return [stat.st_dev, stat.st_ino]


def _hash(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _save(path, data):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def exclusive_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if stream.seek(0, 2) == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise TransferFailure('別の移動処理が実行中です') from error
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


_kernel = ctypes.WinDLL('kernel32', use_last_error=True)
_kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                               wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
_kernel.CreateFileW.restype = wintypes.HANDLE
_kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
_kernel.CloseHandle.restype = wintypes.BOOL
_kernel.SetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
_kernel.SetFileInformationByHandle.restype = wintypes.BOOL


class _Source:
    """DELETE access reserves eventual deletion; no write sharing prevents edits.

    A pre-existing reader without FILE_SHARE_DELETE fails here, before any
    destination is created. Copying reads this very handle, not a second open.
    """
    def __init__(self, path):
        self.path = Path(path)
        handle = _kernel.CreateFileW(str(self.path), 0x80000000 | 0x00010000,
                                     0x00000001 | 0x00000004, None, 3, 0x80, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle
        try:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            _kernel.CloseHandle(handle)
            raise
        self.stream = os.fdopen(fd, 'rb')
        self.stat = os.fstat(self.stream.fileno())
        if not self.stat.st_ino or getattr(self.stat, 'st_file_attributes', 0) & 1:
            self.stream.close()
            raise TransferFailure('元ファイルが読取専用、または識別番号を取得できません', False)
        self.deleted = False

    def disposition(self, delete):
        flag = wintypes.BOOL(bool(delete))  # FILE_DISPOSITION_INFO, FileDispositionInfo=4
        if not _kernel.SetFileInformationByHandle(self.handle, 4, ctypes.byref(flag), ctypes.sizeof(flag)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.deleted = bool(delete)

    def close(self):
        self.stream.close()

    def digest(self):
        self.stream.seek(0)
        value = hashlib.sha256()
        for chunk in iter(lambda: self.stream.read(1024 * 1024), b''):
            value.update(chunk)
        return value.hexdigest()


def _same(path, row, identity_key, digest=True):
    try:
        return (_identity(os.stat(path)) == row[identity_key] and
                (not digest or _hash(path) == row['sha256']))
    except FileNotFoundError:
        return False


def _stage(source, row, journal, record):
    digest = hashlib.sha256()
    source.stream.seek(0)
    with open(row['stage'], 'xb') as output:
        row['stage_id'] = _identity(os.fstat(output.fileno()))
        if not row['stage_id'][1]:
            raise TransferFailure('移動先の識別番号を取得できません', False)
        _save(journal, record)
        for chunk in iter(lambda: source.stream.read(1024 * 1024), b''):
            output.write(chunk)
            digest.update(chunk)
        output.flush()
        os.fsync(output.fileno())
    os.utime(row['stage'], ns=(source.stat.st_atime_ns, source.stat.st_mtime_ns))
    row['sha256'] = digest.hexdigest()
    _save(journal, record)
    if not _same(row['stage'], row, 'stage_id'):
        raise TransferFailure('移動先の一時コピー内容が一致しません')
    current = os.fstat(source.stream.fileno())
    if (current.st_size, current.st_mtime_ns) != (source.stat.st_size, source.stat.st_mtime_ns):
        raise TransferFailure('コピー中に元ファイルが変わりました', False)


def _rollback(record, journal):
    # Originals have not been deleted at this stage. Never remove a published
    # file if we cannot prove that the original is still present and unchanged.
    with contextlib.ExitStack() as stack:
        for row in record['files']:
            source = _Source(row['source'])
            stack.callback(source.close)
            if (_identity(source.stat) != row['source_id'] or
                (row.get('sha256') and source.digest() != row['sha256'])):
                raise TransferFailure('元ファイルの内容/識別が変わったため復旧を保留します', False)
            if row.get('published') and os.path.exists(row['dest']) and not _same(row['dest'], row, 'stage_id'):
                raise TransferFailure('公開済みファイルの内容が変わったため復旧を保留します', False)
            if os.path.exists(row['stage']) and not (row.get('stage_id') and _same(row['stage'], row, 'stage_id', digest=False)):
                raise TransferFailure('一時コピーの所有を確認できません', False)
        for row in reversed(record['files']):
            if os.path.exists(row['dest']) and _same(row['dest'], row, 'stage_id'):
                os.unlink(row['dest'])
            if os.path.exists(row['stage']):
                os.unlink(row['stage'])
        journal.unlink()


def _delete_claimed(sources):
    marked = []
    try:
        for source in sources:
            source.disposition(True)
            marked.append(source)
    except OSError:
        # Keep both destination copies if cleanup is interrupted. Their IDs and
        # hashes are in the journal; a retry resumes cleanup, never publication.
        for source in marked:
            try:
                source.disposition(False)
            except OSError:
                pass
        raise


def _finish_committed(record, journal):
    if not all(_same(row['dest'], row, 'stage_id') for row in record['files']):
        raise TransferFailure('移動確定後のファイルが変更/移動されているため保留します', False)
    with contextlib.ExitStack() as stack:
        sources = []
        for row in record['files']:
            if not os.path.exists(row['source']):
                continue
            source = _Source(row['source'])
            stack.callback(source.close)
            if _identity(source.stat) != row['source_id'] or source.digest() != row['sha256']:
                raise TransferFailure('元の同名ファイルが別の内容に変わっています', False)
            sources.append(source)
        _delete_claimed(sources)
    record.update(phase='done', completed_at=time.time())
    _save(journal, record)
    return True


def _stage_path(dest, ident, index):
    return str(Path(dest).with_name(STAGE_PREFIX + ident + '-' + str(index) + '.part'))


def transfer_group(sources, destinations, journal_dir):
    """Sources/destinations must be in publication order (HDPP before STL).

    journal_dir: ジャーナルとロックを置くフォルダ（ローカルディスク推奨。呼び出し側で指定）
    """
    pairs = [(str(Path(a).absolute()), str(Path(b).absolute())) for a, b in zip(sources, destinations)]
    if not pairs or len(pairs) != len(sources) or len(pairs) != len(destinations):
        raise ValueError('Invalid transfer group')
    if len({os.path.normcase(p) for pair in pairs for p in pair}) != len(pairs) * 2:
        raise TransferFailure('移動元と移動先が重複しています', False)
    root = Path(journal_dir)
    root.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(json.dumps(pairs, ensure_ascii=False).casefold().encode('utf-8')).hexdigest()
    journal = root / (key + '.json')
    with exclusive_lock(root / (key + '.lock')):
        try:
            if journal.exists():
                record = json.loads(journal.read_text(encoding='utf-8'))
                if (record.get('version') != VERSION or
                    [(r['source'], r['dest']) for r in record['files']] != pairs):
                    raise TransferFailure('移動記録の対象が一致しません', False)
                ident = record['id']
                if not isinstance(ident, str) or len(ident) != 32 or any(c not in '0123456789abcdef' for c in ident):
                    raise TransferFailure('移動記録の識別子が不正です', False)
                for i, row in enumerate(record['files']):
                    if row['stage'] != _stage_path(row['dest'], ident, i):
                        raise TransferFailure('一時コピーのパスが一致しません', False)
                if record['phase'] == 'done':
                    if not any(os.path.exists(a) for a, _ in pairs):
                        return True  # Second notification; the completed receipt suffices.
                    if any(os.path.exists(b) for _, b in pairs):
                        raise TransferFailure('移動先に既存の同名ファイルがあります', False)
                    os.rename(journal, root / (key + '-' + ident + '.receipt'))
                elif all(row.get('sha256') and _same(row['dest'], row, 'stage_id') for row in record['files']):
                    return _finish_committed(record, journal)
                elif record['phase'] == 'cleanup':
                    raise TransferFailure('移動確定後の同名ファイルが変更/移動されています', False)
                else:
                    # Restart after interrupted staging or first publication.
                    _rollback(record, journal)
            if any(os.path.exists(dest) for _, dest in pairs):
                raise TransferFailure('移動先に既存の同名ファイルがあります（上書きしません）', False)
            with contextlib.ExitStack() as stack:
                opened = []
                for source, _ in pairs:
                    claim = _Source(source)
                    stack.callback(claim.close)
                    opened.append(claim)
                ident = uuid.uuid4().hex
                record = {'version': VERSION, 'id': ident, 'phase': 'staging', 'created_at': time.time(), 'files': []}
                for i, ((source, dest), claim) in enumerate(zip(pairs, opened)):
                    if _identity(os.stat(source)) != _identity(claim.stat):
                        raise TransferFailure('元ファイル名の対応が変わりました', False)
                    record['files'].append({'source': source, 'dest': dest,
                        'source_id': _identity(claim.stat), 'stage_id': None, 'sha256': None,
                        'stage': _stage_path(dest, ident, i),
                        'published': False})
                _save(journal, record)
                try:
                    for claim, row in zip(opened, record['files']):
                        _stage(claim, row, journal, record)
                    if any(_identity(os.stat(row['source'])) != row['source_id'] for row in record['files']):
                        raise TransferFailure('公開前に元ファイル名の対応が変わりました', False)
                    for row in record['files']:
                        # Windows rename refuses an existing target; no replace/copy fallback.
                        os.rename(row['stage'], row['dest'])
                        row['published'] = True
                        _save(journal, record)
                    record['phase'] = 'cleanup'
                    _save(journal, record)
                except Exception:
                    _rollback(record, journal)
                    raise
                _delete_claimed(opened)
            record.update(phase='done', completed_at=time.time())
            _save(journal, record)
            return True
        except TransferFailure:
            raise
        except (OSError, ValueError, KeyError) as error:
            raise TransferFailure(str(error), not isinstance(error, (ValueError, KeyError, FileExistsError))) from error
