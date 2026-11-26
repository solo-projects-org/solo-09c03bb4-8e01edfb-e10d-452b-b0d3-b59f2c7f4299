"""CSV/JSON parsing and batch validation helpers."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .bounds import aggregate_records


YES_TOKENS = {
    "1", "y", "yes", "true", "t", "是", "有", "合格", "合资格", "有资格",
    "符合", "符合条件", "具备资格", "已领取", "领取", "已领", "已享受",
    "享受", "已发放", "发放", "已发", "已",
}
NO_TOKENS = {
    "0", "n", "no", "false", "f", "否", "无", "没有", "不合格", "不合资格",
    "不符合", "不符", "不具备资格", "不具备", "无资格", "未领取", "未领",
    "未享受", "未发放", "未发", "未",
}
BLANK_TOKENS = {"", "na", "n/a", "null", "none", "unknown", "?", "-", "空", "未知", "未填", "待回访"}


class BatchValidationError(ValueError):
    def __init__(self, message: str, errors: Optional[Sequence[str]] = None):
        super().__init__(message)
        self.message = message
        self.errors = list(errors or [message])


@dataclass(frozen=True)
class HouseholdRecord:
    household_id: str
    street: str
    eligible: Optional[int]
    received: Optional[int]
    row_number: Optional[int] = None

    def as_mapping(self) -> Dict[str, object]:
        return {
            "household_id": self.household_id,
            "street": self.street,
            "eligible": self.eligible,
            "received": self.received,
        }


@dataclass(frozen=True)
class CallbackItem:
    household_id: str
    eligible: Optional[int]
    received: Optional[int]
    source: str = ""


def decode_csv_bytes(content: bytes) -> str:
    for encoding in ("utf-8-sig", "gb18030", "gbk", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def normalize_header(value: object) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[\s_\-/\\()（）:：.．]+", "", text)


def parse_ternary(raw: object, field_label: str = "字段") -> Optional[int]:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return 1 if raw else 0
    if isinstance(raw, int):
        if raw in (0, 1):
            return raw
        raise ValueError(f"{field_label}只允许 1、0 或留空，收到 {raw!r}")

    text = str(raw).strip()
    compact = normalize_header(text)
    if compact in BLANK_TOKENS:
        return None
    if compact in YES_TOKENS:
        return 1
    if compact in NO_TOKENS:
        return 0

    # Tolerant handling of phrases such as “有资格/暂未领取”.
    if any(token in compact for token in (
        "未领取", "未享受", "未发放", "无资格", "不合资格", "不符合", "不符",
        "不具备", "没有", "否", "无", "未",
    )):
        return 0
    if any(token in compact for token in (
        "有资格", "合资格", "符合", "已领取", "已享受", "是", "有", "已",
    )):
        return 1
    raise ValueError(
        f"{field_label}无法识别：{text!r}；请填 是/否、yes/no、1/0 或留空"
    )


def _column_map(header: Sequence[str]) -> Dict[str, int]:
    mapping: Dict[str, int] = {}
    normalized = [normalize_header(h) for h in header]

    exact = {
        "id": {"户号", "家庭户号", "家庭编号", "编号", "住户编号", "householdid", "id", "hhid", "familyid"},
        "street": {"街道", "街道名称", "所在街道", "镇街", "street", "streetname", "area"},
        "eligible": {
            "是否有资格", "有资格", "资格", "是否合资格", "合资格", "是否合格",
            "eligible", "iseligible", "eligibility", "qualified",
        },
        "received": {
            "是否领取", "领取", "领取情况", "是否已领取", "已领取", "领取补贴",
            "received", "hasreceived", "claimed", "claimstatus",
        },
    }

    for field, aliases in exact.items():
        for index, name in enumerate(normalized):
            if name in aliases:
                mapping[field] = index
                break

    if "id" not in mapping:
        for index, name in enumerate(normalized):
            if "户号" in name or "编号" in name or name.endswith("id"):
                mapping["id"] = index
                break
    if "street" not in mapping:
        for index, name in enumerate(normalized):
            if "街道" in name or "街" in name or "street" in name:
                mapping["street"] = index
                break
    if "eligible" not in mapping:
        for index, name in enumerate(normalized):
            if "资格" in name or "eligible" in name or "qualif" in name:
                mapping["eligible"] = index
                break
    if "received" not in mapping:
        for index, name in enumerate(normalized):
            if "领取" in name or "receiv" in name or "claim" in name:
                mapping["received"] = index
                break

    return mapping


def parse_household_csv(content: bytes | str) -> Tuple[List[HouseholdRecord], List[str]]:
    """Parse CSV and return records plus non-fatal/structural parse errors."""

    text = decode_csv_bytes(content) if isinstance(content, bytes) else content
    sample_line = text.splitlines()[0] if text.splitlines() else ""
    if "\t" in sample_line:
        delimiter = "\t"
    elif sample_line.count(";") > sample_line.count(","):
        delimiter = ";"
    else:
        delimiter = ","
    reader = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    errors: List[str] = []
    records: List[HouseholdRecord] = []

    # Remove completely empty lines.
    rows = [row for row in reader if any(str(cell).strip() for cell in row)]
    if not rows:
        return [], ["CSV 为空，或没有任何非空数据行。"]

    first_map = _column_map(rows[0])
    has_header = {"id", "street", "eligible", "received"}.issubset(first_map.keys())

    if has_header:
        columns = first_map
        data_rows = rows[1:]
        line_offset = 2
    else:
        # Headerless fallback: 户号, 街道, 是否有资格, 是否领取.
        if len(rows[0]) < 4:
            return [], [
                "无法识别 CSV 表头。需要包含：户号、街道、是否有资格、是否领取。"
            ]
        columns = {"id": 0, "street": 1, "eligible": 2, "received": 3}
        data_rows = rows
        line_offset = 1

    for offset, row in enumerate(data_rows):
        line_number = offset + line_offset
        if not any(str(cell).strip() for cell in row):
            continue

        def cell(key: str) -> str:
            index = columns[key]
            return row[index].strip() if index < len(row) else ""

        household_id = cell("id")
        street = cell("street")
        try:
            eligible = parse_ternary(cell("eligible"), f"第 {line_number} 行“是否有资格”")
        except ValueError as exc:
            errors.append(str(exc))
            eligible = None
        try:
            received = parse_ternary(cell("received"), f"第 {line_number} 行“是否领取”")
        except ValueError as exc:
            errors.append(str(exc))
            received = None

        if not household_id:
            errors.append(f"第 {line_number} 行：户号不能为空。")
        if not street:
            errors.append(f"第 {line_number} 行：街道不能为空。")
        if received == 1 and eligible == 0:
            errors.append(
                f"第 {line_number} 行（户号 {household_id or '未知'}）："
                "领取者必有资格，但该行填了“无资格/不领取”冲突答案。"
            )

        if household_id and street:
            records.append(
                HouseholdRecord(household_id, street, eligible, received, line_number)
            )

    return records, errors


def validate_import(
    records: Sequence[HouseholdRecord],
    parse_errors: Sequence[str],
    district_total: object,
) -> Tuple[Optional[int], List[str], Optional[Dict[str, object]]]:
    """Validate a complete import batch.

    Returns ``(total, errors, summary)``.  ``total`` is None when the certified
    total itself cannot be parsed.
    """

    errors = list(parse_errors)
    total = parse_district_total(district_total, errors)

    seen: Dict[str, int] = {}
    for record in records:
        if record.household_id in seen:
            errors.append(
                f"户号 {record.household_id} 在 CSV 中重复出现"
                f"（第 {seen[record.household_id]} 行和第 {record.row_number} 行）。"
            )
        else:
            seen[record.household_id] = record.row_number or 0

    summary: Optional[Dict[str, object]] = None
    if records and not any("无法识别" in e or "CSV 为空" in e for e in errors):
        streets = aggregate_records([r.as_mapping() for r in records])
        p_total = sum(s.p for s in streets.values())
        u_total = sum(s.u for s in streets.values())
        summary = {
            "households": len(records),
            "streets": len(streets),
            "minimum_eligible": p_total,
            "maximum_eligible": u_total,
        }
        if total is not None and not (p_total <= total <= u_total):
            errors.append(
                "与全区核定数不相容：已知答案最少可补全出 "
                f"{p_total} 户合资格、最多 {u_total} 户；"
                f"核定数为 {total} 户。"
            )

    return total, errors, summary


def parse_district_total(raw: object, errors: Optional[List[str]] = None) -> Optional[int]:
    if raw is None:
        if errors is not None:
            errors.append("必须填写同批样本核定的全区合资格户数。")
        return None
    if isinstance(raw, int) and not isinstance(raw, bool):
        value = raw
    else:
        text = str(raw).strip().replace(",", "")
        if text == "":
            if errors is not None:
                errors.append("必须填写同批样本核定的全区合资格户数。")
            return None
        try:
            value = int(float(text)) if re.fullmatch(r"\d+\.0+", text) else int(text)
        except ValueError:
            if errors is not None:
                errors.append(f"全区核定数必须是非负整数，收到 {raw!r}。")
            return None
    if value < 0:
        if errors is not None:
            errors.append("全区核定数不能为负数。")
        return None
    return value


def parse_callback_csv(content: str) -> Tuple[List["CallbackItem"], List[str]]:
    """Parse a batch callback CSV with 户号/资格/领取 columns."""

    sample_line = content.splitlines()[0] if content.splitlines() else ""
    if "\t" in sample_line:
        delimiter = "\t"
    elif sample_line.count(";") > sample_line.count(","):
        delimiter = ";"
    else:
        delimiter = ","

    rows = [
        row
        for row in csv.reader(io.StringIO(content), delimiter=delimiter)
        if any(str(cell).strip() for cell in row)
    ]
    if not rows:
        return [], ["回访 CSV 为空。"]

    header_map = _column_map(rows[0])
    has_header = "id" in header_map and (
        "eligible" in header_map or "received" in header_map
    )
    items: List[CallbackItem] = []
    errors: List[str] = []

    if has_header:
        id_col = header_map["id"]
        eligible_col = header_map.get("eligible")
        received_col = header_map.get("received")
        data_rows = rows[1:]
        line_offset = 2
    else:
        if len(rows[0]) < 3:
            return [], ["回访 CSV 至少需要：户号,是否有资格,是否领取。"]
        id_col = 0
        if len(rows[0]) >= 4:
            # Same column order as household import: id, street, eligible, received.
            eligible_col = 2
            received_col = 3
        else:
            eligible_col = 1
            received_col = 2
        data_rows = rows
        line_offset = 1

    for offset, row in enumerate(data_rows):
        line_number = offset + line_offset

        def cell(index: Optional[int]) -> str:
            if index is None or index >= len(row):
                return ""
            return row[index].strip()

        household_id = cell(id_col)
        if not household_id:
            errors.append(f"回访 CSV 第 {line_number} 行：缺少户号。")
            continue
        try:
            eligible = parse_ternary(cell(eligible_col), f"第 {line_number} 行资格")
        except ValueError as exc:
            errors.append(str(exc))
            continue
        try:
            received = parse_ternary(cell(received_col), f"第 {line_number} 行领取")
        except ValueError as exc:
            errors.append(str(exc))
            continue
        items.append(CallbackItem(household_id, eligible, received, f"第 {line_number} 行"))

    return items, errors


def parse_callback_payload(payload: Any) -> Tuple[List[CallbackItem], List[str]]:
    """Parse JSON or form-decoded callback data into items."""

    errors: List[str] = []
    raw_items: List[Tuple[str, Any, Any, str]] = []

    if isinstance(payload, str):
        return parse_callback_csv(payload)
    elif isinstance(payload, bytes):
        return parse_callback_csv(decode_csv_bytes(payload))
    elif isinstance(payload, dict):
        candidate: Any = None
        for key in (
            "answers", "updates", "items", "rows", "callbacks",
            "答案", "回访答案", "回访", "数据",
        ):
            if key in payload:
                candidate = payload[key]
                break
        if candidate is None and payload and all(
            isinstance(v, dict) for v in payload.values()
        ):
            candidate = [dict(household_id=k, **v) for k, v in payload.items()]
        elif candidate is None:
            # A single object for one household.
            if "household_id" in payload or "户号" in payload:
                candidate = [payload]
            else:
                candidate = []

        if isinstance(candidate, dict):
            candidate = [
                dict(household_id=key, **(value if isinstance(value, dict) else {"value": value}))
                for key, value in candidate.items()
            ]
        if not isinstance(candidate, list):
            errors.append("回访数据必须是答案列表，或按户号映射的对象。")
            candidate = []

        for index, item in enumerate(candidate, start=1):
            if not isinstance(item, dict):
                errors.append(f"第 {index} 批答案不是对象：{item!r}")
                continue
            household_id = str(
                item.get("household_id")
                or item.get("户号")
                or item.get("id")
                or item.get("hh_id")
                or ""
            ).strip()
            try:
                eligible = parse_ternary(
                    item.get("eligible", item.get("是否有资格", item.get("eligibility"))),
                    f"第 {index} 项资格",
                )
            except ValueError as exc:
                errors.append(str(exc))
                eligible = "invalid"  # type: ignore[assignment]
            try:
                received = parse_ternary(
                    item.get("received", item.get("是否领取", item.get("claimed"))),
                    f"第 {index} 项领取",
                )
            except ValueError as exc:
                errors.append(str(exc))
                received = "invalid"  # type: ignore[assignment]
            raw_items.append((household_id, eligible, received, f"第 {index} 项"))
    elif isinstance(payload, list):
        return parse_callback_payload({"answers": payload})
    else:
        errors.append("不支持的回访数据格式。")

    items: List[CallbackItem] = []
    for household_id, eligible, received, source in raw_items:
        if not household_id:
            errors.append(f"{source}：缺少户号。")
            continue
        if eligible == "invalid" or received == "invalid":
            continue
        items.append(CallbackItem(household_id, eligible, received, source))
    return items, errors


def merge_callback_items(
    items: Sequence[CallbackItem],
) -> Tuple[Dict[str, Dict[str, Optional[int]]], List[str], int]:
    """Collapse a batch by household.

    Returns merged values, conflict errors, and number of repeated exact
    duplicate field reports inside the submitted batch.
    """

    merged: Dict[str, Dict[str, Optional[int]]] = {}
    errors: List[str] = []
    duplicates = 0

    for item in items:
        current = merged.setdefault(item.household_id, {"eligible": None, "received": None})
        for field in ("eligible", "received"):
            value = getattr(item, field)
            if value is None:
                continue
            if current[field] is None:
                current[field] = value
            elif current[field] == value:
                duplicates += 1
            else:
                label = "是否有资格" if field == "eligible" else "是否领取"
                errors.append(
                    f"户号 {item.household_id} 的批次内{label}出现冲突答案："
                    f"{current[field]} 与 {value}。"
                )
    return merged, errors, duplicates
