"""Flask application factory and HTTP routes."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Tuple

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_file,
)

from .database import (
    database_path,
    dataset_status,
    households_view,
    init_db,
    list_households,
)
from .services import (
    CREDENTIAL_FIELD_KEYS,
    CREDENTIAL_NAME_FRAGMENTS,
    BatchRejected,
    StaleBatchRejected,
    compare,
    import_csv,
    import_sample,
    submit_callbacks,
)


def create_app(config: Dict[str, Any] | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    configured_path = (
        (config or {}).get("DATABASE_PATH")
        or (config or {}).get("DB_PATH")
        or os.environ.get("DATABASE_PATH")
        or os.environ.get("DB_PATH")
        or str(database_path())
    )
    app.config["DATABASE_PATH"] = configured_path
    app.config["JSON_AS_ASCII"] = False
    if config:
        app.config.update(config)
        # Do not let an alternate DB_PATH key diverge from our resolved path.
        app.config["DATABASE_PATH"] = configured_path

    db_path = Path(app.config["DATABASE_PATH"])
    init_db(db_path)

    @app.template_filter("yes_no_blank")
    def yes_no_blank(value: Any) -> str:
        if value is None or value == "":
            return "未填"
        try:
            return "是" if int(value) == 1 else "否"
        except (TypeError, ValueError):
            return str(value)

    @app.context_processor
    def inject_globals() -> Dict[str, Any]:
        return {"app_name": "助餐补贴领取率区间比对"}

    def wants_json() -> bool:
        if request.path.startswith("/api/"):
            return True
        best = request.accept_mimetypes.best if request.accept_mimetypes else None
        return request.is_json or best == "application/json"

    def error_response(exc: BatchRejected) -> Any:
        if wants_json():
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": exc.message,
                        "message": exc.message,
                        "errors": exc.errors,
                        "details": exc.errors,
                        "conflicts": exc.errors,
                    }
                ),
                exc.status_code,
            )
        return (
            render_template(
                "index.html",
                status=dataset_status(db_path),
                import_error=exc.message,
                import_errors=exc.errors,
                form_total=request.values.get("district_total", "8"),
            ),
            exc.status_code,
        )

    @app.get("/healthz")
    def healthz() -> Tuple[Dict[str, str], int]:
        return {"status": "ok"}, 200

    @app.get("/")
    @app.get("/index.html")
    def index() -> str:
        return render_template(
            "index.html",
            status=dataset_status(db_path),
            form_total="8",
        )

    @app.post("/import")
    @app.post("/upload")
    @app.post("/upload_csv")
    @app.post("/datasets")
    @app.post("/api/import")
    @app.post("/api/datasets")
    @app.post("/api/datasets/import")
    def import_route() -> Any:
        content: bytes | str | None = None
        total_raw: Any = None
        source_name = "json"

        def total_from(source: Any) -> Any:
            if not isinstance(source, dict):
                return None
            for key in (
                "district_total",
                "total_eligible",
                "certified_total",
                "verified_total",
                "district_eligible_total",
                "approved_total",
                "全区合资格户数",
                "核定全区合资格户数",
                "全区核定数",
                "核定数",
                "total",
            ):
                if source.get(key) is not None and str(source.get(key)) != "":
                    return source.get(key)
            return None

        if request.is_json:
            data = request.get_json(silent=True) or {}
            content = data.get("csv") or data.get("content") or data.get("data")
            total_raw = total_from(data)
        elif request.files:
            uploaded = None
            for upload_name in ("file", "csv", "import_file", "dataset"):
                candidate = request.files.get(upload_name)
                if candidate is not None and candidate.filename:
                    uploaded = candidate
                    break
            if uploaded is not None:
                content = uploaded.read()
                source_name = uploaded.filename or "upload.csv"
            else:
                content = request.form.get("csv_text") or request.form.get("csv")
            total_raw = total_from(request.form)
        else:
            content = request.form.get("csv_text") or request.form.get("csv")
            total_raw = total_from(request.form)
            source_name = "粘贴CSV"

        if content is None and request.data:
            content = request.get_data(as_text=True)
            source_name = "raw-body"
        if total_raw is None:
            total_raw = total_from(request.args)

        if content is None:
            exc = BatchRejected(
                "没有收到 CSV 文件或内容。",
                ["请选择逐户 CSV，或在 JSON 的 csv 字段中提供 CSV 文本。"],
            )
            return error_response(exc)

        try:
            result = import_csv(db_path, content, total_raw, source_name)
        except BatchRejected as exc:
            return error_response(exc)

        if wants_json():
            return jsonify(result), 200
        return render_template(
            "index.html",
            status=dataset_status(db_path),
            import_result=result,
            form_total=str(result["district_total"]),
        )

    @app.post("/sample/load")
    @app.post("/api/sample/load")
    def sample_load() -> Any:
        try:
            result = import_sample(db_path)
        except BatchRejected as exc:
            return error_response(exc)
        if wants_json() or request.path.startswith("/api/"):
            return jsonify(result), 200
        return render_template(
            "index.html",
            status=dataset_status(db_path),
            import_result=result,
            form_total="8",
            sample_loaded=True,
        )

    @app.get("/sample/csv")
    def sample_csv() -> Any:
        from .services import sample_csv_path

        return send_file(sample_csv_path(), as_attachment=True, download_name="sample_households.csv")

    CALLBACK_EXAMPLE = (
        "户号,是否有资格,是否领取\n"
        "H004,是,\n"
        "H005,否,\n"
        "H102,是,是"
    )

    def extract_batch_credentials() -> list[Any]:
        """Collect every batch token/revision the client sent.

        The server-rendered form includes several alias fields; hidden or
        external clients may send just one conventional name.  Values from
        JSON bodies, form bodies and the query string are all accepted.  An
        empty list means "no batch guard" (the unchanged JSON/API behavior).
        """

        values: list[Any] = []
        chinese_keys = {"批次", "批次号", "调查批次", "数据批次", "版本", "版本号", "数据版本"}

        def consume(source: Any, tolerant: bool) -> None:
            if source is None or not hasattr(source, "items"):
                return
            for key in source.keys():
                lowered = str(key).lower()
                recognized = lowered in CREDENTIAL_FIELD_KEYS or str(key) in chinese_keys
                if tolerant and not recognized:
                    recognized = any(
                        fragment in lowered for fragment in CREDENTIAL_NAME_FRAGMENTS
                    ) or lowered in {"form_id", "page_id", "guard", "stamp"}
                if not recognized:
                    continue
                if hasattr(source, "getlist"):
                    values.extend(source.getlist(key))
                else:
                    values.append(source.get(key))

        if request.is_json:
            # Same recognition as forms.  Payloads without any batch-like key
            # yield an empty list and therefore keep the historical behavior.
            json_body = request.get_json(silent=True) or {}
            consume(json_body, tolerant=True)

            # Also find credentials nested under wrapper objects such as
            # {"context": {"batch_id": "..."}}.  Answer items contain none of
            # the recognized batch/revision key names.
            def walk(node: Any) -> None:
                if isinstance(node, dict):
                    for key, value in node.items():
                        lowered = str(key).lower()
                        recognized = (
                            lowered in CREDENTIAL_FIELD_KEYS
                            or str(key) in chinese_keys
                            or any(
                                fragment in lowered
                                for fragment in CREDENTIAL_NAME_FRAGMENTS
                            )
                        )
                        if recognized and isinstance(value, (str, int)) and not isinstance(value, bool):
                            if value not in (None, ""):
                                values.append(value)
                        else:
                            walk(value)
                elif isinstance(node, list):
                    for item in node:
                        walk(item)

            walk(json_body)
        else:
            consume(request.form, tolerant=True)
            consume(request.args, tolerant=True)

        # Header / double-submit style credentials are accepted too.
        for header in (
            "X-Batch-Id",
            "X-Batch-Token",
            "X-Batch-Revision",
            "X-Dataset-Revision",
            "X-Base-Revision",
            "If-Match",
        ):
            value = request.headers.get(header)
            if value and value.strip() != "*":
                values.append(value)
        return [value for value in values if value not in (None, "")]

    @app.get("/households")
    def households_page() -> str:
        street = request.args.get("street") or None
        # One read snapshot for both the batch token in the form and every row
        # in the same-page table: they can never straddle a re-import.
        status, rows = households_view(db_path, street=street)
        return render_template(
            "households.html",
            status=status,
            households=rows,
            selected_street=street,
            callback_example=CALLBACK_EXAMPLE,
        )

    @app.get("/api/households")
    def api_households() -> Any:
        rows = list_households(db_path, street=request.args.get("street"))
        households = []
        for row in rows:
            item = dict(row)
            item["eligible_implied"] = (
                item["eligible"] == 1
                or (item["eligible"] is None and item["received"] == 1)
            )
            item["received_implied_no"] = (
                item["received"] == 0
                or (item["received"] is None and item["eligible"] == 0)
            )
            households.append(item)
        return jsonify(
            {
                "ok": True,
                "status": dataset_status(db_path),
                "households": households,
            }
        )

    @app.get("/api/state")
    def api_state() -> Any:
        return jsonify({"ok": True, **dataset_status(db_path)})

    def collect_callback_payload() -> Any:
        if request.is_json:
            return request.get_json(silent=True) or {}

        if request.form.get("payload"):
            try:
                return json.loads(request.form["payload"])
            except json.JSONDecodeError:
                return request.form["payload"]

        uploaded = None
        for upload_name in ("callback_file", "file", "csv", "updates"):
            candidate = request.files.get(upload_name)
            if candidate is not None and candidate.filename:
                uploaded = candidate
                break
        if uploaded is not None:
            return uploaded.read().decode("utf-8-sig", errors="replace")

        text = request.form.get("updates_text") or request.form.get("callback_csv")
        if text and text.strip():
            return text

        ids = request.form.getlist("household_id")
        if ids:
            eligible_values = request.form.getlist("eligible")
            received_values = request.form.getlist("received")
            answers = []
            for index, household_id in enumerate(ids):
                answers.append(
                    {
                        "household_id": household_id,
                        "eligible": eligible_values[index] if index < len(eligible_values) else "",
                        "received": received_values[index] if index < len(received_values) else "",
                    }
                )
            return {"answers": answers}

        return {}

    @app.post("/callbacks")
    @app.post("/callback")
    @app.post("/backfill")
    @app.post("/api/callbacks")
    @app.post("/api/callback")
    @app.post("/api/callbacks/batch")
    @app.post("/api/backfill")
    @app.post("/api/visit/batch")
    @app.post("/api/households/callback")
    def callbacks_route() -> Any:
        payload = collect_callback_payload()
        credentials = extract_batch_credentials()
        try:
            result = submit_callbacks(
                db_path,
                payload,
                web_credentials=credentials,
            )
        except StaleBatchRejected as exc:
            if wants_json():
                return jsonify({
                    "ok": False,
                    "error": exc.message,
                    "message": exc.message,
                    "errors": exc.errors,
                    "details": exc.errors,
                    "conflicts": exc.errors,
                    "code": getattr(exc, "code", "stale_batch"),
                    "reason": "stale_batch",
                    "stale_batch": True,
                    "batch_changed": True,
                    "batch_conflict": True,
                    "conflict": True,
                    "current_batch_id": exc.current_batch_id,
                    "current_batch_revision": exc.current_batch_revision,
                }), exc.status_code

            # Re-render from the NEW batch in one consistent snapshot.  Do NOT
            # pass callback_text: the submitted old-batch CSV must never be
            # echoed/filled into the new form.
            status, rows = households_view(db_path)
            return (
                render_template(
                    "households.html",
                    status=status,
                    households=rows,
                    selected_street=None,
                    callback_error=exc.message,
                    callback_errors=exc.errors,
                    batch_changed=True,
                    stale_batch_error=exc,
                    callback_example=CALLBACK_EXAMPLE,
                ),
                exc.status_code,
            )
        except BatchRejected as exc:
            if wants_json():
                return jsonify({
                    "ok": False,
                    "error": exc.message,
                    "message": exc.message,
                    "errors": exc.errors,
                    "details": exc.errors,
                    "conflicts": exc.errors,
                }), exc.status_code
            status, rows = households_view(db_path)
            return (
                render_template(
                    "households.html",
                    status=status,
                    households=rows,
                    selected_street=None,
                    callback_error=exc.message,
                    callback_errors=exc.errors,
                    callback_text=request.form.get("updates_text", ""),
                    callback_example=CALLBACK_EXAMPLE,
                ),
                exc.status_code,
            )

        if wants_json():
            return jsonify(result), 200

        status, rows = households_view(db_path)
        return render_template(
            "households.html",
            status=status,
            households=rows,
            selected_street=None,
            callback_result=result,
            callback_example=CALLBACK_EXAMPLE,
        )

    @app.post("/api/households/<household_id>/callback")
    @app.post("/api/households/<household_id>/backfill")
    def single_household_callback(household_id: str) -> Any:
        data = request.get_json(silent=True) if request.is_json else request.form.to_dict()
        data = data or {}
        payload = {
            "answers": [
                {
                    "household_id": household_id,
                    "eligible": data.get("eligible", data.get("是否有资格")),
                    "received": data.get("received", data.get("是否领取")),
                }
            ]
        }
        try:
            return jsonify(submit_callbacks(db_path, payload)), 200
        except BatchRejected as exc:
            return jsonify({
                "ok": False,
                "error": exc.message,
                "message": exc.message,
                "errors": exc.errors,
                "details": exc.errors,
                "conflicts": exc.errors,
            }), exc.status_code

    def compare_args() -> Tuple[str, str]:
        data: Dict[str, Any] = {}
        if request.is_json:
            data = request.get_json(silent=True) or {}
        source = {**request.args, **request.form, **data}

        def first(*keys: str) -> str:
            for key in keys:
                value = source.get(key)
                if value is not None and str(value).strip():
                    return str(value).strip()
            return ""

        return (
            first("street1", "street_1", "first_street", "street_a", "a"),
            first("street2", "street_2", "second_street", "street_b", "b"),
        )

    @app.route("/compare", methods=["GET", "POST"])
    @app.route("/bounds", methods=["GET", "POST"])
    @app.route("/api/compare", methods=["GET", "POST"])
    @app.route("/api/comparisons", methods=["GET", "POST"])
    @app.route("/api/bounds", methods=["GET", "POST"])
    @app.route("/api/comparison", methods=["GET", "POST"])
    def compare_route() -> Any:
        status = dataset_status(db_path)
        street1, street2 = compare_args()
        result = None
        comparison_error = None

        if street1 and street2:
            try:
                result = compare(db_path, street1, street2)
            except BatchRejected as exc:
                comparison_error = {
                    "error": exc.message,
                    "message": exc.message,
                    "errors": exc.errors,
                    "details": exc.errors,
                }
                if wants_json():
                    return jsonify({"ok": False, **comparison_error}), exc.status_code

        if wants_json() and (street1 and street2):
            if comparison_error:
                return jsonify({"ok": False, **comparison_error}), 400
            assert result is not None
            bounds = {
                "min": result.get("min_value"),
                "max": result.get("max_value"),
                "minimum": result.get("minimum"),
                "maximum": result.get("maximum"),
                "min_detail": result.get("min"),
                "max_detail": result.get("max"),
            }
            return jsonify({
                "ok": True,
                **result,
                "bounds": bounds,
                "result": result,
            }), 200

        if wants_json():
            return (
                jsonify(
                    {
                        "ok": False,
                        "error": "请提供 street1 和 street2。",
                        "streets": status.get("streets", []),
                    }
                ),
                400,
            )

        return render_template(
            "compare.html",
            status=status,
            street1=street1,
            street2=street2,
            result=result,
            comparison_error=comparison_error,
        )

    @app.errorhandler(404)
    def not_found(exc: Exception) -> Any:
        if request.path.startswith("/api/") or wants_json():
            return jsonify({"ok": False, "error": "not found"}), 404
        return render_template("index.html", status=dataset_status(db_path)), 404

    return app
