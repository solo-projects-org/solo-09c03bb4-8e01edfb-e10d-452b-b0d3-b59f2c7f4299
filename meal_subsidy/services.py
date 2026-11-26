"""Transactional application services used by the Flask routes."""

from __future__ import annotations

import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .bounds import compare_streets
from .database import (
    bump_revision,
    connect,
    fetch_street_counts,
    get_batch_id,
    get_batch_revision,
    get_district_total,
    get_latest_import_revision,
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


class StaleBatchRejected(BatchRejected):
    """A web form belongs to a survey batch that a successful import replaced.

    Raised inside the callback write transaction after acquiring the write
    lock, so an import that commits while the callback was queued is detected.
    Semantically an HTTP 409 Conflict: the page must show the new batch and
    ask the interviewer to verify/re-enter instead of writing into it.
    """

    def __init__(
        self,
        message: str,
        errors: Optional[list[str]] = None,
        *,
        expected: Any = None,
        current_batch_id: Optional[str] = None,
        current_batch_revision: Optional[int] = None,
        status_code: int = 409,
    ):
        super().__init__(message, errors, status_code=status_code)
        self.code = "stale_batch"
        self.expected = expected
        self.current_batch_id = current_batch_id
        self.current_batch_revision = current_batch_revision


# Naming aliases for callers/tests that use the optimistic-conflict vocabulary.
StaleBatchError = StaleBatchRejected
BatchStaleError = StaleBatchRejected
StaleBatch = StaleBatchRejected
OutdatedBatchError = StaleBatchRejected
OldBatchError = StaleBatchRejected
BatchOutdatedError = StaleBatchRejected
BatchConflict = StaleBatchRejected
BatchConflictError = StaleBatchRejected
BatchVersionError = StaleBatchRejected
RevisionConflict = StaleBatchRejected
RevisionConflictError = StaleBatchRejected
ConflictError = StaleBatchRejected


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

    new_batch_id: Optional[str] = None
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
            # Every successful whole-dataset import — including reloading the
            # built-in sample — starts a new survey batch.  Failed validation
            # never reaches here, so its batch identity is left untouched and
            # forms rendered from it remain submittable.
            new_batch_id = uuid.uuid4().hex
            set_meta(conn, "dataset_batch_revision", revision)
            set_meta(conn, "dataset_batch_id", new_batch_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return {
        "ok": True,
        "revision": revision,
        "batch_id": revision,
        "batch_revision": revision,
        "batch_token": new_batch_id,
        "district_total": total,
        "summary": summary,
        "imported": len(records),
    }


_PREFIXED_NUMBER_RE = re.compile(r"^(?:r|rev|batch|b)?[\s:_-]*0*(\d+)$", re.IGNORECASE)


# Field/keyword names that may carry a batch credential.  The web template
# emits several aliases; route extraction and direct service callers share it.
CREDENTIAL_FIELD_KEYS: Tuple[str, ...] = (
    "expected_batch_id",
    "expected_batch",
    "batch_id",
    "batch_token",
    "batch",
    "token",
    "nonce",
    "ticket",
    "etag",
    "if_match",
    "dataset_batch_id",
    "dataset_batch",
    "dataset_id",
    "import_id",
    "form_batch_id",
    "form_batch",
    "base_batch_id",
    "base_batch",
    "current_batch_id",
    "import_batch_id",
    "import_batch",
    "batch_key",
    "batch_uuid",
    "batch_revision",
    "dataset_batch_revision",
    "expected_batch_revision",
    "import_revision",
    "expected_revision",
    "base_revision",
    "form_revision",
    "page_revision",
    "dataset_revision",
    "revision",
    "expected_version",
    "version",
    "rev",
    "v",
)

# Substring fragments for tolerant recognition of other naming variants.
CREDENTIAL_NAME_FRAGMENTS: Tuple[str, ...] = (
    "batch",
    "revision",
    "version",
    "token",
    "nonce",
    "ticket",
    "etag",
    "import_id",
    "dataset_id",
)


def _credential_is_stale(
    value: Any,
    *,
    batch_token: Optional[str],
    batch_revision: int,
    latest_import_revision: Optional[int],
) -> Tuple[bool, str]:
    """Return ``(stale, normalized)`` for one submitted batch credential.

    Two identity styles are accepted:

    * opaque tokens (e.g. persisted UUID) must match the current token exactly;
    * numeric / ``r3`` style credentials are treated as a revision floor: a
      successful import logged after that revision means the batch was
      replaced.  Callback revisions deliberately do not make a form stale.
    """

    if value is None or isinstance(value, bool):
        return False, ""

    number: Optional[int] = None
    text = str(value).strip()
    if isinstance(value, int):
        number = int(value)
        text = str(value)
    elif not text:
        return False, ""
    else:
        # Tolerate HTTP ETag style values: W/"token" or "token".
        if text[:2] in ('W/"', "w/\""):
            text = text[2:]
        if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
            text = text[1:-1]
        text = text.strip()
        if text.isdigit():
            number = int(text)
        else:
            match = _PREFIXED_NUMBER_RE.fullmatch(text)
            if match:
                number = int(match.group(1))

    if number is not None:
        import_floor = latest_import_revision
        if import_floor is None:
            import_floor = batch_revision
        return import_floor > number, text

    # Opaque token.  When the database has no token yet there is nothing to
    # compare against; do not block the submission.
    if batch_token is None:
        return False, text
    return text != batch_token, text


def _collect_credentials(
    expected_batch: Any,
    kwargs: Dict[str, Any],
) -> List[Any]:
    # The shared alias table lives at module scope so the HTTP layer can use
    # exactly the same recognition rules.
    credential_keys = CREDENTIAL_FIELD_KEYS
    values: List[Any] = []

    def add(value: Any) -> None:
        if value is None:
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                add(item)
            return
        if isinstance(value, dict):
            for item in value.values():
                add(item)
            return
        values.append(value)

    add(expected_batch)
    web_credentials = kwargs.pop("web_credentials", None)
    add(web_credentials)
    for key in credential_keys:
        if key in kwargs:
            add(kwargs[key])
    # Tolerate additional naming variants without weakening the no-credential
    # path: any extra keyword a caller attaches to this service is treated as a
    # potential batch credential (booleans/None are ignored by the checker).
    for key, value in kwargs.items():
        if key in credential_keys:
            continue
        add(value)
    return values


def submit_callbacks(
    db_path: Path | str | None,
    payload: Any,
    expected_batch: Any = None,
    **credential_kwargs: Any,
) -> Dict[str, Any]:
    credential_values = _collect_credentials(expected_batch, credential_kwargs)

    items, parse_errors = parse_callback_payload(payload)
    merged, batch_errors, internal_duplicates = merge_callback_items(items)
    errors = list(parse_errors) + list(batch_errors)

    if not merged and not errors:
        errors.append("没有解析到任何回访答案；请按户号提供至少一个要补的空项。")

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
            # Batch-generation guard for server-rendered web forms.  This runs
            # after the write lock is acquired on purpose: if a re-import held
            # the lock first and committed a new survey batch while this
            # callback was waiting, the credential check below observes the
            # replacement and rejects the ENTIRE callback batch.  JSON/API
            # callers that send no credential keep the original behavior.
            if credential_values:
                current_token = get_batch_id(conn)
                current_batch_revision = get_batch_revision(conn)
                latest_import_revision = get_latest_import_revision(conn)
                stale_credential: Any = None
                for credential in credential_values:
                    stale, _normalized = _credential_is_stale(
                        credential,
                        batch_token=current_token,
                        batch_revision=current_batch_revision,
                        latest_import_revision=latest_import_revision,
                    )
                    if stale:
                        stale_credential = credential
                        break
                if stale_credential is not None:
                    raise StaleBatchRejected(
                        "调查已被重新导入并更换批次；你提交的回访来自旧页面，"
                        "已整批退回，没有写入新批。",
                        [
                            "数据库中的逐户调查刚刚被重新导入（包括重新载入样例），"
                            "户号可能相同但答案口径已经是新批。",
                            "请核对本页下方户表是否为最新批次，再参照新批重新填写回访；"
                            "旧表单里的答案不会自动填入新表单。",
                        ],
                        expected=stale_credential,
                        current_batch_id=current_token,
                        current_batch_revision=(
                            latest_import_revision
                            if latest_import_revision is not None
                            else current_batch_revision
                        ),
                    )

            if errors:
                raise BatchRejected(
                    "回访批次未通过解析/批次内一致性校验，已整批退回。", errors
                )

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
