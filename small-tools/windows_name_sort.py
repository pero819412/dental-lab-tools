"""Windows（エクスプローラー）と同じ「名前順」で並べるためのソートキー。

痒いところ: Python の sorted() だと「file10」が「file2」より前に来るなど、エクスプローラーの
表示順と食い違う。Windows では shlwapi の StrCmpLogicalW をそのまま使い、完全に同じ順にする。
Windows 以外では、数字の自然順・全角半角・カタカナ/ひらがなを揃えた近似キーにフォールバックする。

Explorer-compatible display sorting, not a canonical identity or search key.
Use the current Windows user's native logical comparison without normalizing
away punctuation, width, or kana differences. Keep a portable natural-order
fallback for environments where the Windows DLL is unavailable.

標準ライブラリのみ（ctypes）。例: sorted(names, key=explorer_name_sort_key)
"""
import ctypes
from ctypes import wintypes
from functools import cmp_to_key
import os
import re
import unicodedata


def _load_windows_compare():
    if os.name != "nt":
        return None
    try:
        compare = ctypes.WinDLL("shlwapi.dll").StrCmpLogicalW
        compare.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        compare.restype = ctypes.c_int
        return compare
    except (AttributeError, OSError):
        return None


_WINDOWS_COMPARE = _load_windows_compare()
_WINDOWS_KEY = cmp_to_key(_WINDOWS_COMPARE) if _WINDOWS_COMPARE is not None else None


def _portable_name_sort_key(value):
    normalized = unicodedata.normalize("NFKC", value).casefold()
    kana_folded = "".join(
        chr(ord(character) - 0x60)
        if (
            0x30A1 <= ord(character) <= 0x30F6
            or 0x30FD <= ord(character) <= 0x30FE
        )
        else character
        for character in normalized
    )
    significant = next(
        (
            character
            for character in kana_folded
            if character.isalnum()
            or "ぁ" <= character <= "ゟ"
            or "㐀" <= character <= "䶿"
            or "一" <= character <= "鿿"
            or "豈" <= character <= "﫿"
        ),
        "",
    )
    if significant.isascii() and significant.isalnum():
        character_group = 0
    elif "ぁ" <= significant <= "ゟ" or significant == "ー":
        character_group = 1
    elif (
        "㐀" <= significant <= "䶿"
        or "一" <= significant <= "鿿"
        or "豈" <= significant <= "﫿"
    ):
        character_group = 2
    else:
        character_group = 3
    natural_parts = tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", kana_folded)
        if part
    )
    return character_group, natural_parts, normalized


def explorer_name_sort_key(value):
    """Return a sort key using Windows' logical order for display names.

    Comparisons intentionally follow this machine's Windows collation. Do not
    reuse this key for source-order matching, hashes, or persistent identifiers.
    The native key also supports equality in compound (date/count, name) keys.
    """
    text = str(value or "")
    if _WINDOWS_KEY is not None:
        return _WINDOWS_KEY(text)
    return _portable_name_sort_key(text)


if __name__ == "__main__":
    import sys

    for name in sorted(sys.argv[1:] or ["file10.stl", "file2.stl", "File1.stl"], key=explorer_name_sort_key):
        print(name)
