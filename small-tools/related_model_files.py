"""関連モデルファイルの収集: 同じ症例キーを持つ「設計ファイル」と「Model_*.stl」を相互に探す。

痒いところ: 製作物 …_XXX-000000-00-1.stl とモデル …_XXX-000000-00-2.stl のように、
枝番だけ違うファイルを同じ症例としてまとめて移動・確認したい。ファイル名に埋め込まれた
症例キー（<接頭辞>-<セグメント1>-<セグメント2>）で照合し、フォルダ直下だけを探す。標準ライブラリのみ。

症例キーの接頭辞は運用（受注番号の体系）ごとに違うので、CASE_KEY_PREFIX か
build_case_key_pattern() で自分の形式に合わせること。
"""
import os
import re
from pathlib import Path


DESIGN_OUTPUT_EXTENSIONS = frozenset({".stl", ".hdpp", ".wncdw"})
MODEL_FILE_PREFIX = "model_"
# 症例キーの接頭辞（ダミー）。自分の受注番号の接頭辞に置き換える。
CASE_KEY_PREFIX = "CASE"


def build_case_key_pattern(prefix=CASE_KEY_PREFIX):
    """<prefix>-<英数字>-<英数字> を症例キーとして取り出す正規表現を作る。

    例（prefix="CASE"）: 製作物 ..._CASE-000000-00-1 とモデル ..._CASE-000000-00-2 は、
    先頭2セグメント（CASE-000000-00）を同じ症例として扱う。
    各セグメントは桁数を固定せず、英数字のどちらも許可する。
    """
    return re.compile(
        rf"(?<![A-Z0-9])(?P<key>{re.escape(prefix)}-[A-Z0-9]+-[A-Z0-9]+)(?=-|[^A-Z0-9]|$)",
        re.IGNORECASE,
    )


CASE_KEY_PATTERN = build_case_key_pattern()


def extract_common_case_key(value, pattern=None):
    """Return the canonical case key embedded in a path or filename."""
    filename = os.path.basename(os.fspath(value or ""))
    match = (pattern or CASE_KEY_PATTERN).search(filename)
    if match is None:
        return ""
    return match.group("key").upper()


def is_model_stl_path(value):
    path = Path(os.fspath(value or ""))
    return path.suffix.casefold() == ".stl" and path.name.casefold().startswith(
        MODEL_FILE_PREFIX
    )


def is_design_output_path(value):
    """Return True for a movable design output, excluding Model_ STL files."""
    path = Path(os.fspath(value or ""))
    return (
        path.suffix.casefold() in DESIGN_OUTPUT_EXTENSIONS
        and not is_model_stl_path(path)
    )


def find_related_design_files(model_path, source_dir, pattern=None):
    """Find top-level design outputs sharing a selected model's case key."""
    case_key = extract_common_case_key(model_path, pattern)
    if not case_key:
        return []

    directory = Path(source_dir)
    try:
        candidates = list(directory.iterdir())
    except OSError:
        return []

    related = [
        candidate
        for candidate in candidates
        if candidate.is_file()
        and is_design_output_path(candidate)
        and extract_common_case_key(candidate, pattern) == case_key
    ]
    return sorted(related, key=lambda candidate: candidate.name.casefold())


def find_related_standby_models(design_path, standby_dir, pattern=None):
    """Find top-level Model_*.stl files in standby_dir sharing the design's case key.

    standby_dir: モデルを置いている待機フォルダ（必須。元実装はツール内の固定フォルダだった）
    """
    if is_model_stl_path(design_path):
        return []

    case_key = extract_common_case_key(design_path, pattern)
    if not case_key:
        return []

    directory = Path(standby_dir)
    try:
        candidates = list(directory.iterdir())
    except OSError:
        return []

    related = [
        candidate
        for candidate in candidates
        if candidate.is_file()
        and is_model_stl_path(candidate)
        and extract_common_case_key(candidate, pattern) == case_key
    ]
    return sorted(related, key=lambda candidate: candidate.name.casefold())
