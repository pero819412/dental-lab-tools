"""hdppエンコーダーの中核ロジック。

exocad の .constructionInfo（XML）を読み、歯式（FDI）・製作種別・材料・マージン点群から
DWOS 互換の .hdpp（XML）を組み立てる。GUI・設定・ファイル出力には依存しない。

注意: parse_construction_info() は参照 STL の存在確認・形式検査・ハッシュ照合のために
ファイルを読む。ジョブ生成と不可分な検査なので、ここに置いている（EXTRACTION_NOTES.md 参照）。
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Iterable, Mapping

LOGGER = logging.getLogger("hdpp_encoder")

DATE_PREFIX_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}_")
INVALID_FILENAME_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
# FDI 表記の前歯（中切歯・側切歯・犬歯）
FRONT_TEETH = {11, 12, 13, 21, 22, 23, 31, 32, 33, 41, 42, 43}

# 材料グループ → .hdpp の <material> に書く名前。
# CAM 側（DWOS）の材料ライブラリに登録されている名称と一致している必要がある。
# 材料名はラボごとに異なるため、既定値は持たない（すべて未設定）。
# 自分のラボで使用している名称を設定ファイルの "material_names" に
# 記入すること（README / config.example.json 参照）。
# 未設定のグループを使おうとすると material_name() が EncoderError を送出する。
DEFAULT_MATERIAL_NAMES: dict[str, str] = {
    "ZR": "",
    "CC": "",
    "METAL": "",
    "PEEK": "",
    "WAX": "",
    "TEMP": "",
}
_material_names: dict[str, str] = dict(DEFAULT_MATERIAL_NAMES)

HDPP_CREATOR_SYSTEM = "DWOS"
HDPP_CREATOR_VERSION = "16.2.4"


class EncoderError(RuntimeError):
    pass


def configure_material_names(overrides: Mapping[str, object] | None) -> None:
    """設定ファイルの material_names で .hdpp 材料名を上書きする。"""
    _material_names.clear()
    _material_names.update(DEFAULT_MATERIAL_NAMES)
    for group, name in (overrides or {}).items():
        key = str(group or "").strip().upper()
        value = str(name or "").strip()
        if key in DEFAULT_MATERIAL_NAMES and value:
            _material_names[key] = value


def material_name(group: str) -> str:
    name = _material_names[group]
    if not name:
        raise EncoderError(
            f"材料名が未設定です: material_names.{group} に、自分のラボの"
            "CAM材料ライブラリで使用している名称を設定してください"
            "（config.example.json 参照）"
        )
    return name


@dataclass(frozen=True)
class MaterialProfile:
    group: str
    hdpp_name: str
    source_code: str
    source_name: str


@dataclass
class MarginLoop:
    margin_type: str
    points: list[tuple[float, float, float]]


@dataclass
class ToothMargin:
    number: int
    reconstruction_type: str
    tooth_type: str
    margin_type: str
    anatomy_prefix: str
    direction: tuple[float, float, float]
    points: list[tuple[float, float, float]]
    additional_margins: list[MarginLoop] = field(default_factory=list)


@dataclass
class ConstructionJob:
    source_stl: Path
    output_stem: str
    material: MaterialProfile
    tooth_numbers: list[int]
    margins: list[ToothMargin]
    is_bridge: bool
    declared_sha1: str
    declared_sha256: str
    color: str
    part_type: str


# ---------------------------------------------------------------------------
# 文字列・ファイル名
# ---------------------------------------------------------------------------

def clean_path(value: object) -> str:
    return str(value or "").strip().strip('"')


def clean_staff_name(value: object) -> str:
    name = str(value or "").strip()
    name = INVALID_FILENAME_PATTERN.sub("_", name)
    return name.strip(" ._")[:40]


def safe_filename_stem(value: str) -> str:
    value = INVALID_FILENAME_PATTERN.sub("_", value)
    value = value.strip().rstrip(". ")
    if not value:
        value = "exocad_part"
    if len(value) > 220:
        value = value[:220].rstrip(". ")
    return value


def append_staff_name(output_stem: str, staff_name: object) -> str:
    stem = safe_filename_stem(output_stem)
    name = clean_staff_name(staff_name)
    if not name:
        return stem

    suffix = f"_{name}"
    if stem.casefold().endswith(suffix.casefold()):
        return stem
    maximum_base_length = max(1, 220 - len(suffix))
    return safe_filename_stem(f"{stem[:maximum_base_length].rstrip('. ')}{suffix}")


def remove_date_prefix(stem: str) -> str:
    """先頭の YYYY-MM-DD_ を1回だけ除去する。"""
    return DATE_PREFIX_PATTERN.sub("", stem, count=1)


# ---------------------------------------------------------------------------
# XML・幾何
# ---------------------------------------------------------------------------

def element_text(parent: ET.Element | None, name: str, default: str = "") -> str:
    if parent is None:
        return default
    child = parent.find(name)
    if child is None or child.text is None:
        return default
    return child.text.strip()


def parse_vector(parent: ET.Element | None, name: str) -> tuple[float, float, float] | None:
    if parent is None:
        return None
    child = parent.find(name)
    if child is None:
        return None
    try:
        values = (
            float(element_text(child, "x")),
            float(element_text(child, "y")),
            float(element_text(child, "z")),
        )
    except ValueError:
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    length = math.sqrt(sum(value * value for value in values))
    if length <= 1e-12:
        return None
    return tuple(value / length for value in values)


def parse_matrix(
    parent: ET.Element | None,
    name: str,
) -> tuple[tuple[float, float, float, float], ...] | None:
    if parent is None:
        return None
    child = parent.find(name)
    if child is None:
        return None
    try:
        rows = tuple(
            tuple(float(element_text(child, f"_{row}{column}")) for column in range(4))
            for row in range(4)
        )
    except ValueError:
        return None
    if not all(math.isfinite(value) for row in rows for value in row):
        return None
    return rows


def multiply_matrices(
    first: tuple[tuple[float, float, float, float], ...],
    second: tuple[tuple[float, float, float, float], ...],
) -> tuple[tuple[float, float, float, float], ...]:
    return tuple(
        tuple(
            sum(first[row][index] * second[index][column] for index in range(4))
            for column in range(4)
        )
        for row in range(4)
    )


def transform_points(
    points: list[tuple[float, float, float]],
    matrix: tuple[tuple[float, float, float, float], ...],
) -> list[tuple[float, float, float]]:
    transformed: list[tuple[float, float, float]] = []
    for point in points:
        source = (*point, 1.0)
        result = tuple(
            sum(source[row] * matrix[row][column] for row in range(4))
            for column in range(4)
        )
        if not all(math.isfinite(value) for value in result) or abs(result[3]) <= 1e-12:
            raise EncoderError("マージン座標の変換結果が不正です。")
        transformed.append(tuple(value / result[3] for value in result[:3]))
    return transformed


def points_are_equal(
    first: tuple[float, float, float],
    second: tuple[float, float, float],
    tolerance: float = 1e-10,
) -> bool:
    return all(abs(left - right) <= tolerance for left, right in zip(first, second))


def parse_margin_tag_points(
    tooth: ET.Element,
    margin_tag: str,
) -> list[tuple[float, float, float]]:
    points: list[tuple[float, float, float]] = []
    for point in tooth.findall(f"./{margin_tag}/Vec3"):
        try:
            values = (
                float(element_text(point, "x")),
                float(element_text(point, "y")),
                float(element_text(point, "z")),
            )
        except ValueError as error:
            raise EncoderError(
                f"{margin_tag}のマージン点に数値以外の値があります。"
            ) from error
        if not all(math.isfinite(value) for value in values):
            raise EncoderError(
                f"{margin_tag}のマージン点に有限値ではない座標があります。"
            )
        points.append(values)
    if len(points) < 3:
        return []
    if not points_are_equal(points[0], points[-1]):
        points.append(points[0])
    LOGGER.info(
        "歯式%sのマージンに%sを使用します: %d点",
        element_text(tooth, "Number", "?"),
        margin_tag,
        len(points),
    )
    return points


def parse_margin_points(tooth: ET.Element) -> list[tuple[float, float, float]]:
    for margin_tag in (
        "Margin",
        "PreparationMargin",
        "MarginImplantGeometry",
        "MarginEmergenceProfile",
    ):
        points = parse_margin_tag_points(tooth, margin_tag)
        if points:
            return points
    return []


# ---------------------------------------------------------------------------
# 製作種別・材料の判定
# ---------------------------------------------------------------------------

def classify_tooth(
    reconstruction_type: str,
    margin_bottom_type: str,
    *,
    implant_type: str = "",
    is_screw_retained: str = "",
    filename: str = "",
    has_axis_implant: bool = False,
) -> tuple[str, str, str]:
    """(tooth_type, margin_type, anatomy_prefix) を返す。"""
    value = f"{reconstruction_type} {margin_bottom_type}".lower()
    normalized_implant_type = implant_type.strip().lower()
    normalized_screw_retained = is_screw_retained.strip().lower()
    filename_lower = filename.lower()
    compact_implant_type = re.sub(r"[^a-z0-9]", "", normalized_implant_type)
    is_without_abutment = (
        compact_implant_type == "withoutabutment"
        or "without_abutment" in filename_lower
    )
    if is_without_abutment:
        return "crown", "coping", "imp"
    is_implant = (
        any(token in value for token in ("abutment", "implant"))
        or normalized_implant_type not in ("", "none", "false", "0", "no")
        or normalized_screw_retained in ("true", "1", "yes")
        or any(
            token in filename_lower
            for token in ("without_abutment", "abutment", "implant")
        )
        or has_axis_implant
    )
    if is_implant:
        return "abutment", "emergence", "imp"
    if any(token in value for token in ("inlay", "onlay", "tabletop", "table_top")):
        return "inlay", "onlay", "in"
    if any(token in value for token in ("coping", "reduced", "framework", "thimble")):
        return "coping", "coping", "cop"
    return "crown", "coping", "cr"


def normalize_material_text(*values: str) -> str:
    joined = " ".join(value for value in values if value)
    return re.sub(r"[^A-Z0-9一-龠ぁ-んァ-ヶ]+", "", joined.upper())


def _profile(group: str, source_code: str, source_name: str) -> MaterialProfile:
    return MaterialProfile(group, material_name(group), source_code, source_name)


def classify_material(source_code: str, source_name: str) -> MaterialProfile:
    normalized = normalize_material_text(source_code, source_name)
    if any(token in normalized for token in ("ZI", "ZR", "ZIRCON", "ジルコニア")):
        return _profile("ZR", source_code, source_name)
    if "PEEK" in normalized:
        return _profile("PEEK", source_code, source_name)
    if any(token in normalized for token in ("WAX", "ワックス")):
        return _profile("WAX", source_code, source_name)
    if any(token in normalized for token in ("PMMA", "TEMP", "PROVISIONAL", "TEK", "テンポラリー")):
        return _profile("TEMP", source_code, source_name)
    if any(token in normalized for token in ("TITANIUM", "TITAN", "COCR", "CRCO", "NPM", "METAL", "チタン", "金属")):
        return _profile("METAL", source_code, source_name)
    if any(
        token in normalized
        for token in (
            "CADCAM",
            "COMPOSITE",
            "HYBRID",
            "CERASMART",
            "ENAMIC",
            "LAVULTIMATE",
            "ハイブリッド",
        )
    ):
        return _profile("CC", source_code, source_name)
    raise EncoderError(
        "未対応の材料です。誤った振り分けを防ぐため出力を中止しました: "
        f"Material={source_code or '(空)'} / MaterialName={source_name or '(空)'}"
    )


def material_profile_from_override(value: str) -> MaterialProfile:
    """constructionInfo に材料がないとき、外部（D&D 等）で指定された材料を使う。"""
    key = normalize_material_text(value)
    table = {
        "ZR": ("ZR", "ZR"),
        "CC": ("CC", "CC"),
        "TI": ("METAL", "Ti"),
        "METAL": ("METAL", "Ti"),
        "WAX": ("WAX", "WAX"),
        "TEK": ("TEMP", "TEK"),
        "TEMP": ("TEMP", "TEK"),
        "PEEK": ("PEEK", "PEEK"),
    }
    try:
        group, code = table[key]
    except KeyError as error:
        raise EncoderError(f"D&Dで指定された材料に対応していません: {value}") from error
    return _profile(group, code, "D&D指定")


def representative_anatomy(margins: list[ToothMargin]) -> str:
    prefixes = {margin.anatomy_prefix for margin in margins}
    for candidate in ("imp", "in", "cop", "cr"):
        if candidate in prefixes:
            return candidate
    return "cr"


def output_prefix(
    material: MaterialProfile,
    margins: list[ToothMargin],
    tooth_numbers: list[int],
    is_bridge: bool,
) -> str:
    anatomy = representative_anatomy(margins)
    if material.group == "ZR":
        return anatomy
    if material.group == "CC":
        if anatomy == "in":
            return "inCC"
        if is_bridge:
            return "CCbr"
        if tooth_numbers and all(number in FRONT_TEETH for number in tooth_numbers):
            return "CCML"
        return "CC"
    if material.group == "METAL":
        if anatomy == "in":
            return "Tiin"
        if is_bridge:
            return "Tibr"
        if anatomy == "cop":
            return "TiHR"
        return "Ti"
    if material.group == "PEEK":
        return "PEEK"
    if material.group == "WAX":
        return "WAX"
    if material.group == "TEMP":
        return "temp"
    raise EncoderError(f"未対応の材料グループです: {material.group}")


KNOWN_OUTPUT_PREFIXES = (
    "inCC_",
    "CCbr_",
    "CCML_",
    "CC_",
    "TiHRbr_",
    "TiHR_",
    "Tibr_",
    "Tiin_",
    "Ti_",
    "PEEK_",
    "WAX_",
    "in_",
    "cr_",
    "cop_",
    "imp_",
    "temp_",
)


def output_stem_for_job(
    source_stl: Path,
    prefix: str,
    tooth_numbers: list[int],
    margins: list[ToothMargin],
) -> str:
    """元 STL 名から「材料/種別プレフィックス_元名-歯式-種別_cad」を作る。"""
    stem = remove_date_prefix(source_stl.stem)
    for known in KNOWN_OUTPUT_PREFIXES:
        if stem.lower().startswith(known.lower()):
            stem = stem[len(known):]
            break

    lowered = stem.lower()
    if tooth_numbers and not any(
        re.search(rf"(?<!\d){number}(?!\d)", stem) for number in tooth_numbers
    ):
        stem += "-" + "_".join(str(number) for number in tooth_numbers)

    anatomy = representative_anatomy(margins)
    anatomy_word = {"cr": "crown", "in": "inlay", "cop": "coping", "imp": "abutment"}[anatomy]
    if not any(word in lowered for word in ("crown", "inlay", "onlay", "coping", "abutment", "bridge")):
        stem += f"-{anatomy_word}"
    if not stem.lower().endswith("_cad"):
        stem += "_cad"
    return safe_filename_stem(f"{prefix}_{stem}")


# ---------------------------------------------------------------------------
# STL・ハッシュ検査
# ---------------------------------------------------------------------------

def file_hash(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_case_file(case_folder: Path, filename: str) -> Path:
    basename = PureWindowsPath(filename).name
    direct = case_folder / basename
    if direct.is_file():
        return direct
    basename_lower = basename.lower()
    for candidate in case_folder.iterdir():
        if candidate.is_file() and candidate.name.lower() == basename_lower:
            return candidate
    raise EncoderError(f"constructionInfoが参照するSTLを確認できません: {basename}")


def validate_stl(path: Path) -> None:
    size = path.stat().st_size
    if size < 84:
        raise EncoderError(f"STLが小さすぎるか、書き込み途中です: {path.name}")
    with path.open("rb") as handle:
        header = handle.read(84)
    triangle_count = struct.unpack("<I", header[80:84])[0]
    if size == 84 + triangle_count * 50:
        return
    if header[:5].lower() == b"solid":
        return
    raise EncoderError(f"STLの形式またはファイルサイズを確認できません: {path.name}")


def validate_declared_hashes(
    source_stl: Path,
    declared_sha1: str,
    declared_sha256: str,
) -> None:
    if declared_sha1 and file_hash(source_stl, "sha1").lower() != declared_sha1.lower():
        raise EncoderError(f"STLのSHA-1がconstructionInfoと一致しません: {source_stl.name}")
    if declared_sha256 and file_hash(source_stl, "sha256").lower() != declared_sha256.lower():
        raise EncoderError(f"STLのSHA-256がconstructionInfoと一致しません: {source_stl.name}")


# ---------------------------------------------------------------------------
# constructionInfo の解析
# ---------------------------------------------------------------------------

def parse_tooth_number(tooth: ET.Element) -> int | None:
    try:
        return int(element_text(tooth, "Number"))
    except ValueError:
        return None


def create_tooth_margin(
    tooth: ET.Element,
    fallback_direction: tuple[float, float, float] | None,
    construction: ET.Element | None = None,
    filename: str = "",
) -> ToothMargin | None:
    number = parse_tooth_number(tooth)
    if number is None:
        return None
    reconstruction_type = element_text(tooth, "ReconstructionType")
    # A pontic has no preparation/implant interface margin. The bridge-wide
    # screw-retained flag must not turn this intermediate tooth into an abutment.
    # Keep its tooth number and geometry in the ConstructionJob/unchanged STL.
    if "pontic" in reconstruction_type.casefold():
        LOGGER.info("歯式%sはポンティックのため支台マージンの生成対象から除外します。", number)
        return None
    margin_bottom_type = element_text(tooth, "MarginBottomType")
    implant_type = element_text(tooth, "ImplantType")
    # A merged bridge may contain natural and implant supports. Ignore only
    # construction-wide hints when this member has explicit natural-tooth data;
    # incomplete/contradictory implant data must still fail the original checks.
    member_count = len(construction.findall("./ToothNumbers/int")) if construction is not None else 0
    explicit_natural_member = (
        member_count > 1
        and implant_type.strip().casefold() == "none"
        and not any(token in f"{reconstruction_type} {margin_bottom_type}".casefold() for token in ("abutment", "implant"))
        and tooth.find("AxisImplant") is None
        and tooth.find("AxisScrew") is None
        and len(tooth.findall("./Margin/Vec3")) >= 3
        and not tooth.findall("./MarginEmergenceProfile/Vec3")
        and not tooth.findall("./MarginImplantGeometry/Vec3")
    )
    if explicit_natural_member:
        LOGGER.info("歯式%sは歯単位の天然歯データを使用し、ブリッジ全体のインプラント判定を適用しません。", number)
    tooth_type, margin_type, anatomy_prefix = classify_tooth(
        reconstruction_type,
        margin_bottom_type,
        implant_type=implant_type,
        is_screw_retained="" if explicit_natural_member else element_text(construction, "IsScrewRetained"),
        filename="" if explicit_natural_member else filename,
        has_axis_implant=tooth.find("AxisImplant") is not None,
    )
    additional_margins: list[MarginLoop] = []
    if tooth_type == "abutment":
        points = parse_margin_tag_points(tooth, "MarginEmergenceProfile")
        abutmentbase_points = parse_margin_tag_points(tooth, "MarginImplantGeometry")
        if not points or not abutmentbase_points:
            raise EncoderError(
                f"歯式{number}のインプラント用マージンが不足しています。"
                "MarginEmergenceProfileとMarginImplantGeometryの両方が必要です。"
            )
        additional_margins.append(
            MarginLoop(margin_type="abutmentbase", points=abutmentbase_points)
        )
        direction = (
            parse_vector(tooth, "AxisImplant")
            or parse_vector(tooth, "AxisScrew")
            or parse_vector(tooth, "Axis")
            or fallback_direction
        )
    elif anatomy_prefix == "imp":
        points = parse_margin_tag_points(tooth, "PreparationMargin")
        scan_matrix = parse_matrix(construction, "MatrixToScanDataFiles")
        rotation_matrix = parse_matrix(construction, "ZRotationMatrix") or parse_matrix(
            tooth,
            "ZRotationMatrix",
        )
        if not points or scan_matrix is None or rotation_matrix is None:
            raise EncoderError(
                f"歯式{number}の支台読みインプラント用マージンを変換できません。"
                "PreparationMargin、MatrixToScanDataFiles、ZRotationMatrixが必要です。"
            )
        points = transform_points(
            points,
            multiply_matrices(scan_matrix, rotation_matrix),
        )
        direction = (0.0, 0.0, 1.0)
        LOGGER.info(
            "歯式%sを支台読みインプラントとして処理します: crown/coping %d点",
            number,
            len(points),
        )
    else:
        points = parse_margin_points(tooth)
        if not points:
            return None
        direction = parse_vector(tooth, "Axis") or fallback_direction
    if direction is None:
        raise EncoderError(f"歯式{number}の挿入方向を取得できません。")
    return ToothMargin(
        number=number,
        reconstruction_type=reconstruction_type,
        tooth_type=tooth_type,
        margin_type=margin_type,
        anatomy_prefix=anatomy_prefix,
        direction=direction,
        points=points,
        additional_margins=additional_margins,
    )


def parse_construction_info(
    construction_path: Path,
    material_override: str = "",
) -> list[ConstructionJob]:
    case_folder = construction_path.parent
    try:
        root = ET.parse(construction_path).getroot()
    except (ET.ParseError, OSError) as error:
        raise EncoderError(f"constructionInfoを読み込めません: {error}") from error

    teeth_by_number: dict[int, ET.Element] = {}
    for tooth in root.findall("./Teeth/Tooth"):
        number = parse_tooth_number(tooth)
        if number is not None:
            teeth_by_number[number] = tooth

    constructions = root.findall("./ConstructionFileList/ConstructionFile")
    if not constructions:
        raise EncoderError("constructionInfoにConstructionFileがありません。")

    jobs: list[ConstructionJob] = []
    for construction in constructions:
        filename = element_text(construction, "Filename")
        if not filename:
            raise EncoderError("ConstructionFileのFilenameが空です。")
        source_stl = find_case_file(case_folder, filename)
        validate_stl(source_stl)

        declared_sha1 = element_text(construction, "FileHashSHA1")
        declared_sha256 = element_text(construction, "FileHashSHA256")
        validate_declared_hashes(source_stl, declared_sha1, declared_sha256)

        tooth_numbers: list[int] = []
        for item in construction.findall("./ToothNumbers/int"):
            try:
                tooth_numbers.append(int((item.text or "").strip()))
            except ValueError:
                continue
        if not tooth_numbers:
            tooth_numbers = sorted(teeth_by_number)

        fallback_direction = parse_vector(construction, "EstimatedDirectionForCAM")
        margins: list[ToothMargin] = []
        for tooth_number in tooth_numbers:
            tooth = teeth_by_number.get(tooth_number)
            if tooth is None:
                continue
            margin = create_tooth_margin(
                tooth,
                fallback_direction,
                construction=construction,
                filename=source_stl.name,
            )
            if margin is not None:
                margins.append(margin)
        if not margins:
            LOGGER.warning(
                "マージン点を含む歯牙情報がないためConstructionFileをスキップします: %s",
                source_stl.name,
            )
            continue

        source_code = element_text(construction, "Material")
        source_name = element_text(construction, "MaterialName")
        if source_code or source_name:
            material = classify_material(source_code, source_name)
        elif material_override:
            material = material_profile_from_override(material_override)
        else:
            material = classify_material(source_code, source_name)
        color = element_text(construction, "MaterialColor")
        if not color:
            for tooth_number in tooth_numbers:
                tooth = teeth_by_number.get(tooth_number)
                if tooth is None:
                    continue
                color = element_text(tooth, "ToothColor")
                if color:
                    break
        part_type = element_text(construction, "PartType")
        is_bridge = (
            len(margins) > 1
            or "bridge" in part_type.lower()
            or "bridge" in source_stl.stem.lower()
            or any("pontic" in margin.reconstruction_type.lower() for margin in margins)
        )
        prefix = output_prefix(material, margins, tooth_numbers, is_bridge)
        stem = output_stem_for_job(source_stl, prefix, tooth_numbers, margins)

        jobs.append(
            ConstructionJob(
                source_stl=source_stl,
                output_stem=stem,
                material=material,
                tooth_numbers=tooth_numbers,
                margins=margins,
                is_bridge=is_bridge,
                declared_sha1=declared_sha1,
                declared_sha256=declared_sha256,
                color=color,
                part_type=part_type,
            )
        )
    if not jobs:
        raise EncoderError("マージン点を含む変換対象のSTLがありません。")
    return jobs


def select_construction_info(case_folder: Path) -> list[Path]:
    files = sorted(
        (
            path
            for path in case_folder.iterdir()
            if path.is_file() and path.suffix.lower() == ".constructioninfo"
        ),
        key=lambda path: (path.stat().st_mtime_ns, path.name.lower()),
    )
    if not files:
        raise EncoderError("患者フォルダに.constructionInfoがありません。")
    return files


# ---------------------------------------------------------------------------
# .hdpp の組み立て
# ---------------------------------------------------------------------------

def stable_identifier(value: str, length: int = 14) -> str:
    return hashlib.sha1(value.encode("utf-8", errors="replace")).hexdigest().upper()[:length]


def format_vector(values: Iterable[float]) -> str:
    return " ".join(f"{value:.8f}" for value in values)


def append_user_property(parent: ET.Element, tag: str, value: str) -> None:
    child = ET.SubElement(parent, tag, {"name": tag})
    child.text = value


def build_hdpp_xml(job: ConstructionJob, case_folder: Path) -> bytes:
    root = ET.Element("Part")
    creator = ET.SubElement(root, "creator")
    ET.SubElement(creator, "system").text = HDPP_CREATOR_SYSTEM
    ET.SubElement(creator, "version").text = HDPP_CREATOR_VERSION

    teeth = ET.SubElement(root, "teeth")
    for margin in job.margins:
        tooth = ET.SubElement(teeth, "tooth", {"type": margin.tooth_type})
        margin_loops = [
            MarginLoop(margin_type=margin.margin_type, points=margin.points),
            *margin.additional_margins,
        ]
        for margin_loop in margin_loops:
            margin_node = ET.SubElement(
                tooth,
                "margin",
                {"type": margin_loop.margin_type},
            )
            is_abutment_loop = (
                margin.tooth_type == "abutment"
                and margin_loop.margin_type in {"emergence", "abutmentbase"}
            )
            if not is_abutment_loop:
                ET.SubElement(margin_node, "direction").text = format_vector(
                    margin.direction
                )
            points_node = ET.SubElement(margin_node, "points")
            for point in margin_loop.points:
                ET.SubElement(points_node, "p").text = format_vector(point)
            if not is_abutment_loop:
                ET.SubElement(margin_node, "undercut").text = "0"

    partdata = ET.SubElement(root, "partdata")
    ET.SubElement(partdata, "material").text = job.material.hdpp_name

    userproperties = ET.SubElement(root, "userproperties")
    append_user_property(
        userproperties,
        "Color",
        "" if job.color in ("", "---") else job.color,
    )
    append_user_property(userproperties, "partdirections", "5X")
    part_hash_source = f"{case_folder.name}|{job.source_stl.name}|{job.declared_sha256}"
    order_hash_source = f"{case_folder.name}|{case_folder.resolve()}"
    append_user_property(userproperties, "partid", stable_identifier(part_hash_source))
    append_user_property(userproperties, "partorderid", stable_identifier(order_hash_source))
    append_user_property(
        userproperties,
        "partordername",
        remove_date_prefix(case_folder.name)[:120],
    )
    append_user_property(
        userproperties,
        "partordercomment",
        "Generated by hdpp_encoder.py from exocad constructionInfo. "
        "Verify margin, material and insertion direction before machining.",
    )

    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode", short_empty_elements=False)
    return ('<?xml version="1.0" encoding="UTF-8"?>\r\n' + body + "\r\n").encode("utf-8")
