"""シェード名変換: リネーム中に「新しく打ち込んだ部分」だけのシェード表記を自動で揃える。

痒いところ: ファイル名にシェードガイド表記（例 3M2, 2L1.5）を打つと、材料に応じた
VITA classical 表記（例 A2 / A2-B2）へ自動変換。既存部分や明示表記（A1-B1 等）は触らない。

ルールは INI ファイル（[guide_to_vita] [shift_materials] [shift_output] [nonshift_output]）。
場所は 環境変数 SHADE_RENAME_RULES → このファイルと同じフォルダの shade_rename_rules.ini の順。
見本: shade_rename_rules.example.ini。標準ライブラリのみで動作。
"""
import configparser
import os
import re
from pathlib import Path


DEFAULT_RULES_PATH = Path(__file__).resolve().parent / "shade_rename_rules.ini"
AMBIGUOUS_SHADE = "Bright/Light"


def _default_rules_path():
    explicit = os.environ.get("SHADE_RENAME_RULES", "").strip()
    return Path(explicit) if explicit else DEFAULT_RULES_PATH


def _load_section(parser, section):
    if not parser.has_section(section):
        return {}
    return {
        str(key).strip().upper(): str(value).strip()
        for key, value in parser.items(section)
        if str(key).strip() and str(value).strip()
    }


def load_shade_rename_rules(path=None):
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        with open(path or _default_rules_path(), "r", encoding="utf-8-sig") as rules_file:
            parser.read_file(rules_file)
    except OSError:
        return {
            "guide_to_vita": {},
            "shift_materials": set(),
            "shift_output": {},
            "nonshift_output": {},
        }
    return {
        "guide_to_vita": _load_section(parser, "guide_to_vita"),
        "shift_materials": set(
            _load_section(parser, "shift_materials")
        ),
        "shift_output": _load_section(parser, "shift_output"),
        "nonshift_output": _load_section(parser, "nonshift_output"),
    }


RULES = load_shade_rename_rules()


def reload_rules(path=None):
    """別のルールファイルを読み直す（テストや設定変更時）。"""
    RULES.clear()
    RULES.update(load_shade_rename_rules(path))
    return RULES


_ALREADY_SHIFTED_PATTERN = re.compile(
    r"(?<![A-Z0-9.])A(?:1|2)-(?:B[1-4]|C[1-4]|D[1-4])"
    r"(?![A-Z0-9.])",
    re.IGNORECASE,
)
_EXPLICIT_SHADE_CONTEXT = re.compile(
    r"(?:A(?:1|2|3(?:\.5)?|4)[DL]|A(?:1|2)-[BCD][1-4])(?![A-Z0-9.])",
    re.IGNORECASE,
)


def _changed_inserted_span(original, edited):
    prefix_length = 0
    prefix_limit = min(len(original), len(edited))
    while (
        prefix_length < prefix_limit
        and original[prefix_length] == edited[prefix_length]
    ):
        prefix_length += 1

    suffix_length = 0
    suffix_limit = min(
        len(original) - prefix_length,
        len(edited) - prefix_length,
    )
    while (
        suffix_length < suffix_limit
        and original[len(original) - suffix_length - 1]
        == edited[len(edited) - suffix_length - 1]
    ):
        suffix_length += 1

    inserted_end = len(edited) - suffix_length
    if inserted_end <= prefix_length:
        return None
    return prefix_length, inserted_end, edited[prefix_length:inserted_end]


def _is_token_boundary(text, index):
    if index < 0 or index >= len(text):
        return True
    character = text[index]
    return not (character.isalnum() or character == ".")


def _has_material_prefix_before(text, token_start):
    before_token = text[:token_start]
    return bool(
        re.search(
            r"(?:^|[^A-Z0-9])([A-Z]{1,10})$",
            before_token,
            flags=re.IGNORECASE,
        )
    )


def _find_shade_token(inserted_text, edited_context=None, inserted_start=0):
    if _ALREADY_SHIFTED_PATTERN.search(inserted_text):
        return None

    guide_to_vita = RULES["guide_to_vita"]
    direct_vita = {
        key: key
        for key in (
            set(RULES["shift_output"])
            | set(RULES["nonshift_output"])
        )
    }
    candidates = {**direct_vita, **guide_to_vita}
    uppercase_text = inserted_text.upper()
    context = inserted_text if edited_context is None else edited_context
    protected_spans = [match.span() for match in _EXPLICIT_SHADE_CONTEXT.finditer(context)]
    best_match = None
    for source, vita in candidates.items():
        start = 0
        while True:
            position = uppercase_text.find(source, start)
            if position < 0:
                break
            end = position + len(source)
            explicit_notation = any(
                inserted_start + position >= left and inserted_start + end <= right
                for left, right in protected_spans
            )
            if (
                not explicit_notation
                and
                (
                    _is_token_boundary(inserted_text, position - 1)
                    or _has_material_prefix_before(inserted_text, position)
                )
                and _is_token_boundary(inserted_text, end)
            ):
                candidate = (position, -len(source), source, vita, end)
                if best_match is None or candidate < best_match:
                    best_match = candidate
            start = position + 1
    if best_match is None:
        return None
    position, _negative_length, source, vita, end = best_match
    return {
        "start": position,
        "end": end,
        "source": source,
        "vita": vita,
    }


def _shift_material_before(edited_name, token_start):
    before_token = edited_name[:token_start]
    materials = sorted(RULES["shift_materials"], key=len, reverse=True)
    if not materials:
        return ""
    material_pattern = "|".join(re.escape(material) for material in materials)
    match = re.search(
        rf"(?<![A-Z0-9])({material_pattern})[_\s-]*$",
        before_token,
        flags=re.IGNORECASE,
    )
    return match.group(1).upper() if match else ""


def convert_new_shade_in_name(original_name, edited_name):
    """Convert only the text newly entered during the current rename."""
    original_name = str(original_name or "")
    edited_name = str(edited_name or "")
    result = {
        "name": edited_name,
        "changed": False,
        "source": "",
        "vita": "",
        "final": "",
        "material": "",
        "manual_choice": False,
    }
    inserted_span = _changed_inserted_span(original_name, edited_name)
    if inserted_span is None:
        return result

    inserted_start, inserted_end, inserted_text = inserted_span
    token = _find_shade_token(inserted_text, edited_name, inserted_start)
    if token is None:
        return result

    result["source"] = token["source"]
    result["vita"] = token["vita"]
    if token["vita"].casefold() == AMBIGUOUS_SHADE.casefold():
        result["manual_choice"] = True
        result["final"] = token["vita"]
        return result

    absolute_token_start = inserted_start + token["start"]
    material = _shift_material_before(edited_name, absolute_token_start)
    result["material"] = material
    if material:
        final_shade = RULES["shift_output"].get(token["vita"], token["vita"])
    else:
        final_shade = RULES["nonshift_output"].get(
            token["vita"],
            token["vita"],
        )
    result["final"] = final_shade

    converted_inserted = (
        inserted_text[:token["start"]]
        + final_shade
        + inserted_text[token["end"]:]
    )
    converted_name = (
        edited_name[:inserted_start]
        + converted_inserted
        + edited_name[inserted_end:]
    )
    result["name"] = converted_name
    result["changed"] = converted_name != edited_name
    return result


def convert_new_shade_in_filename(original_filename, edited_filename):
    original_stem, _original_extension = os.path.splitext(
        str(original_filename or "")
    )
    edited_stem, edited_extension = os.path.splitext(str(edited_filename or ""))
    result = convert_new_shade_in_name(original_stem, edited_stem)
    result["name"] = result["name"] + edited_extension
    return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print("usage: shade_name_converter.py <元のファイル名> <編集後のファイル名>")
        raise SystemExit(2)
    print(convert_new_shade_in_filename(sys.argv[1], sys.argv[2]))
