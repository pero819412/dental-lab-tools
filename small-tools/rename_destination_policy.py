"""リネーム先ポリシー: 「名前を変えずに Enter」したときの振り分けを、元ファイルだけで判定する。

痒いところ: 画面に今どのタブが見えているかに左右されず、選択中の元ファイルがすべて
「個人作業フォルダ内の、マーカー付き設計ファイル（.stl/.hdpp）」のときだけ待機扱いにする。
パスの字面だけで判定し、ネットワークやファイルにはアクセスしない。標準ライブラリのみ。

Choose the unchanged-Enter shortcut from source files, never the visible tab.
"""
import ntpath
import re

# 待機扱いにするファイル名の先頭トークン（元環境では光学印象ケースの目印）。
# 自分の運用に合わせて変更する。
DEFAULT_MARKER = "光"
DEFAULT_EXTENSIONS = frozenset({".stl", ".hdpp"})


def _absolute_key(value):
    value = str(value or "").strip()
    if not value or value.startswith(("\\\\?\\", "\\\\.\\")):
        return ""
    if not ntpath.isabs(value) or not ntpath.splitdrive(value)[0]:
        return ""
    return ntpath.normcase(ntpath.normpath(value))


def unchanged_enter_standby_allowed(
    entries,
    personal_root,
    marker=DEFAULT_MARKER,
    extensions=DEFAULT_EXTENSIONS,
):
    """All sources must be marked design files inside the personal folder.

    entries: [{"path": "C:\\...\\光_xxx.stl", "is_dir": False}, ...]
    personal_root: 個人作業フォルダ（Windows の絶対パス）

    Lexical Windows-path checks require no network/file access. They are a UI
    routing decision, not an authorization to follow links or move other files.
    """
    root = _absolute_key(personal_root)
    if not root or not entries or not marker:
        return False
    marker_pattern = re.compile(rf"^{re.escape(marker)}(?:[\s_]|$)")
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("is_dir"):
            return False
        path = _absolute_key(entry.get("path"))
        if not path or ntpath.splitext(path)[1] not in extensions:
            return False
        # The marker is a leading token. A name merely containing the marker
        # character must not route an ordinary case to standby.
        if not marker_pattern.match(ntpath.basename(path)):
            return False
        try:
            if ntpath.commonpath((path, root)) != root:
                return False
        except ValueError:
            return False
    return True
