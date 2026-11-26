"""Transactional application services used by the Flask routes."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Optional

from .bounds import compare_streets
from .database import (
    bump_revision,
    connect,
    fetch_street_counts,
    get_district_total,
    get_revision,
    set_meta,
    take_snapshot,
)
from .importer import (
    merge_callback_items,
    parse_callback_payload,
    parse_household_csv,
    validate_import,
)


class BatchRejected(Exception):
    """Raised when an import/callback batch is incompatible.

    The HTTP layer renders ``errors``; no successful mutation from the batch
    is committed.
    """

    def __init__(self, message: str, errors: Optional[list[str]] = None, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.errors = errors or [message]
        self.status_code = status_code


def import_csv(
    db_path: Path | str | None,
    content: bytes | str,
    district_total_raw: object,
    source_name: str = "csv",
) -> Dict[str, Any]:
    records, parse_errors = parse_household_csv(content)
    total, errors, summary = validate_import(records, parse_errors, district_total_raw)
    if errors:
        raise BatchRejected("导入批次未通过校验，已整批退回；原数据未改动。", errors)

    assert total is not None
    assert summary is not None

    with closing(connect(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("DELETE FROM households")
            conn.executemany(
                """
                INSERT INTO households(household_id, street, eligible, received)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (r.household_id, r.street, r.eligible, r.received)
                    for r in records
                ],
            )
            set_meta(conn, "district_eligible_total", total)
            revision = bump_revision(
                conn,
                "import",
                len(records),
                f"导入 {source_name}：{len(records)} 户",
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return {
        "ok": True,
        "revision": revision,
        "district_total": total,
        "summary": summary,
        "imported": len(records),
    }


def submit_callbacks(db_path: Path | str | None, payload: Any) -> Dict[str, Any]:
    items, parse_errors = parse_callback_payload(payload)
    merged, batch_errors, internal_duplicates = merge_callback_items(items)
    errors = list(parse_errors) + list(batch_errors)

    if not merged and not errors:
        errors.append("没有解析到任何回访答案；请按户号提供至少一个要补的空项。")
    if errors:
        raise BatchRejected("回访批次未通过解析/批次内一致性校验，已整批退回。", errors)

    with closing(connect(db_path)) as conn:
        # An immediate write transaction serializes concurrent batches.  All
        # current effective answers are read only after acquiring the lock.
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            raise BatchRejected(
                "数据库正被其他回填占用，请重试。",
                [f"获取写入事务失败：{exc}"],
                status_code=409,
            ) from exc

        try:
            total = get_district_total(conn)
            if total is None:
                raise BatchRejected(
                    "尚未导入 CSV 和全区核定数，无法核对回访是否相容。",
                    ["请先导入逐户 CSV 与全区合资格户数。"],
                )

            household_ids = list(merged.keys())
            placeholders = ",".join("?" for _ in household_ids)
            rows = conn.execute(
                f"SELECT * FROM households WHERE household_id IN ({placeholders})",
                household_ids,
            ).fetchall()
            existing: Dict[str, Dict[str, Any]] = {
                row["household_id"]: dict(row) for row in rows
            }

            changes: list[tuple[str, str, int]] = []
            repeated_existing = 0
            households_changed: set[str] = set()

            for household_id, proposed in merged.items():
                row = existing.get(household_id)
                if row is None:
                    errors.append(f"户号 {household_id} 不在当前生效数据中，不能补空项。")
                    continue

                for field in ("eligible", "received"):
                    value = proposed.get(field)
                    if value is None:
                        continue
                    current = row[field]
                    if current is None:
                        changes.append((household_id, field, int(value)))
                        row[field] = int(value)
                        households_changed.add(household_id)
                    elif int(current) == int(value):
                        # Same answer reported again: idempotent/no increment.
                        repeated_existing += 1
                    else:
                        label = "是否有资格" if field == "eligible" else "是否领取"
                        errors.append(
                            f"户号 {household_id} 的{label}已有生效答案 "
                            f"{_yes_no(current)}，不能改为 {_yes_no(value)}；"
                            "补空项不能覆盖原答案。"
                        )

            # Cross-field implication after combining this batch with all
            # already effective answers.  Only batch rows can newly violate it;
            # committed rows already passed this check and the table CHECK.
            for household_id, row in existing.items():
                if row["eligible"] == 0 and row["received"] == 1:
                    errors.append(
                        f"户号 {household_id}：合并本批后出现“无资格却领取”，"
                        "违反领取者必有资格。"
                    )

            if errors:
                raise BatchRejected(
                    "回访批次与已生效答案/规则冲突，已整批退回；原数据照查。",
                    errors,
                    status_code=400,
                )

            # Apply inside the transaction.  If the district-total check below
            # fails, ROLLBACK removes every row of this batch.
            for household_id, field, value in changes:
                conn.execute(
                    f"UPDATE households SET {field}=?, updated_at=CURRENT_TIMESTAMP "
                    "WHERE household_id=?",
                    (value, household_id),
                )

            # Aggregate the merged table using the same SQL categories as the
            # comparison snapshot and check the certified district total.
            streets = fetch_street_counts(conn)
            p_total = sum(s.p for s in streets.values())
            u_total = sum(s.u for s in streets.values())
            if not (p_total <= total <= u_total):
                errors.append(
                    "与已经生效的答案及全区核定数合并后不相容："
                    f"合资格户数只能在 {p_total}–{u_total} 之间补全，"
                    f"但全区核定数为 {total}。"
                )

            if errors:
                raise BatchRejected(
                    "回访批次与已生效答案及全区核定数冲突，已整批退回；原数据照查。",
                    errors,
                    status_code=400,
                )

            if changes:
                revision = bump_revision(
                    conn,
                    "callback_batch",
                    len(changes),
                    f"{len(households_changed)} 户，新补 {len(changes)} 项",
                )
            else:
                revision = get_revision(conn)

            conn.execute("COMMIT")
        except BatchRejected:
            conn.execute("ROLLBACK")
            raise
        except Exception:
            conn.execute("ROLLBACK")
            raise

    unique_answers = sum(
        sum(1 for value in values.values() if value is not None)
        for values in merged.values()
    )
    return {
        "ok": True,
        "revision": revision,
        "unique_households": len(merged),
        "unique_answers": unique_answers,
        "newly_filled": len(changes),
        "changed_fields": len(changes),
        "filled_count": len(changes),
        "accepted_updates": len(changes),
        "unchanged_answers": repeated_existing,
        "duplicate_reports_in_batch": internal_duplicates,
        "households_changed": len(households_changed),
        "idempotent_repeat": len(changes) == 0,
        "message": (
            f"整批生效：新补 {len(changes)} 个空项，"
            f"{repeated_existing} 项与已生效答案相同而未增计。"
        ),
    }


def compare(
    db_path: Path | str | None, street_1: str, street_2: str
) -> Dict[str, Any]:
    snapshot = take_snapshot(db_path)
    total = snapshot["district_total"]
    if total is None:
        raise BatchRejected(
            "尚未设置全区核定数。",
            ["请先导入逐户 CSV 和全区合资格户数。"],
            status_code=400,
        )

    # Snapshot is fully detached here; no DB transaction is held during this
    # CPU calculation, so legal callback batches can commit concurrently.
    result = compare_streets(
        snapshot["streets"],
        int(total),
        street_1.strip(),
        street_2.strip(),
        revision=snapshot["revision"],
    )
    return result.to_dict()


def sample_csv_path() -> Path:
    return Path(__file__).resolve().parent.parent / "sample_data" / "sample_households.csv"


def import_sample(db_path: Path | str | None) -> Dict[str, Any]:
    path = sample_csv_path()
    content = path.read_bytes()
    return import_csv(db_path, content, 8, source_name="可试样例")


def _yes_no(value: Any) -> str:
    if value is None:
        return "空"
    try:
        return "是" if int(value) == 1 else "否"
    except (TypeError, ValueError):
        return str(value)
