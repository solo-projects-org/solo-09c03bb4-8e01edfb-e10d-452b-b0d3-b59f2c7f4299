"""Regression tests for web callback batch binding.

The interviewer's /households form and its table must be one survey batch.
A successful re-import (sample reload included) invalidates old forms as a
whole, while failed imports, same-batch callback commits and restarts on the
same database do not.  The JSON callback API keeps its no-token behavior.
"""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from meal_subsidy import create_app  # noqa: E402
from meal_subsidy.database import (  # noqa: E402
    bump_revision,
    connect,
    set_meta,
)
from meal_subsidy.services import (  # noqa: E402
    BatchRejected,
    StaleBatchRejected,
    submit_callbacks,
)

CALLBACK_CSV = (
    "户号,是否有资格,是否领取\n"
    "H004,是,\n"
    "H005,否,\n"
    "H102,是,是\n"
)


def hidden_value(html: str, name: str) -> str | None:
    match = re.search(
        r'name=["\']%s["\'][^>]*value=["\']([^"\']*)["\']' % re.escape(name),
        html,
    )
    return match.group(1) if match else None


class BatchBindingWebTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="subsidy-batch-", suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        self.app = create_app({"DATABASE_PATH": self.db_path, "TESTING": True})
        self.client = self.app.test_client()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def test_form_and_table_carry_same_batch(self):
        self.assertEqual(self.client.post("/api/sample/load").status_code, 200)
        html = self.client.get("/households").text

        batch_id = hidden_value(html, "batch_id")
        batch_token = hidden_value(html, "batch_token")
        self.assertIsNotNone(batch_id)
        self.assertTrue(batch_token)

        form_token = re.search(r'<form[^>]*data-batch-token="([^"]+)"', html)
        table_token = re.search(r'<table[^>]*data-batch-token="([^"]+)"', html)
        self.assertIsNotNone(form_token)
        self.assertIsNotNone(table_token)
        self.assertEqual(form_token.group(1), table_token.group(1))
        self.assertEqual(form_token.group(1), batch_token)

        form_batch = re.search(r'<form[^>]*data-batch-id="([^"]+)"', html)
        table_batch = re.search(r'<table[^>]*data-batch-id="([^"]+)"', html)
        self.assertEqual(form_batch.group(1), table_batch.group(1))
        self.assertEqual(form_batch.group(1), batch_id)

        state = self.client.get("/api/state").get_json()
        self.assertEqual(str(state["batch_id"]), batch_id)
        self.assertEqual(state["batch_token"], batch_token)

    def test_reimport_rejects_old_batch_and_does_not_refill_form(self):
        self.client.post("/api/sample/load")
        old_html = self.client.get("/households").text
        old_batch = hidden_value(old_html, "batch_id")
        old_token = hidden_value(old_html, "batch_token")

        before = self.client.get(
            "/api/compare?street1=人民路街道&street2=建设路街道"
        ).get_json()

        # Tab B reloads the sample: a new batch with identical household IDs.
        self.assertEqual(self.client.post("/sample/load").status_code, 200)

        response = self.client.post(
            "/callbacks",
            data={
                "updates_text": CALLBACK_CSV,
                "batch_id": old_batch,
                "batch_token": old_token,
            },
        )
        self.assertEqual(response.status_code, 409)
        html = response.text

        # Prompt asks to verify the new batch.
        self.assertIn("新批", html)
        self.assertIn("核对", html)
        self.assertIn('data-testid="batch-stale"', html)
        self.assertIn('data-testid="callback-error"', html)

        # The textarea of the new form is empty and the old CSV is not echoed.
        textarea = re.search(
            r'<textarea[^>]*id="updates_text"[^>]*>(.*?)</textarea>',
            html,
            re.S,
        )
        self.assertIsNotNone(textarea)
        self.assertEqual(textarea.group(1), "")
        self.assertNotIn(CALLBACK_CSV, html)

        # Nothing was written into the new batch.
        state = self.client.get("/api/state").get_json()
        self.assertEqual(state["revision"], 2)
        households = self.client.get("/api/households").get_json()["households"]
        h004 = next(h for h in households if h["household_id"] == "H004")
        self.assertIsNone(h004["eligible"])

        after = self.client.get(
            "/api/compare?street1=人民路街道&street2=建设路街道"
        ).get_json()
        self.assertEqual(before["min"]["text"], after["min"]["text"])
        self.assertEqual(before["max"]["text"], after["max"]["text"])
        self.assertEqual(after["revision"], 2)

        # The fresh token from the rejected page works.
        fresh_batch = hidden_value(html, "batch_id")
        fresh_token = hidden_value(html, "batch_token")
        retry = self.client.post(
            "/callbacks",
            data={
                "updates_text": CALLBACK_CSV,
                "batch_id": fresh_batch,
                "batch_token": fresh_token,
            },
        )
        self.assertEqual(retry.status_code, 200)
        self.assertIn('data-testid="newly-filled">4<', retry.text)

    def test_failed_reimport_keeps_old_form_valid(self):
        self.client.post("/api/sample/load")
        batch_id = hidden_value(self.client.get("/households").text, "batch_id")

        bad_csv = "户号,街道,是否有资格,是否领取\nX1,测试街,是,是\n"
        bad = self.client.post(
            "/api/import", json={"csv": bad_csv, "district_total": 999}
        )
        self.assertEqual(bad.status_code, 400)

        ok = self.client.post(
            "/callbacks",
            data={"updates_text": CALLBACK_CSV, "batch_id": batch_id},
        )
        self.assertEqual(ok.status_code, 200)
        self.assertIn('data-testid="newly-filled">4<', ok.text)

    def test_other_same_batch_callback_keeps_old_form_valid(self):
        self.client.post("/api/sample/load")
        html = self.client.get("/households").text
        batch_id = hidden_value(html, "batch_id")
        token = hidden_value(html, "batch_token")

        other = self.client.post(
            "/api/callbacks",
            json={"answers": [{"household_id": "H006", "eligible": "是"}]},
        )
        self.assertEqual(other.status_code, 200)

        ok = self.client.post(
            "/callbacks",
            data={
                "updates_text": CALLBACK_CSV,
                "batch_id": batch_id,
                "batch_token": token,
            },
        )
        self.assertEqual(ok.status_code, 200)

    def test_restart_on_same_database_keeps_form_valid(self):
        self.client.post("/api/sample/load")
        html = self.client.get("/households").text
        batch_id = hidden_value(html, "batch_id")
        token = hidden_value(html, "batch_token")

        restarted = create_app({"DATABASE_PATH": self.db_path, "TESTING": True})
        client = restarted.test_client()
        ok = client.post(
            "/callbacks",
            data={
                "updates_text": CALLBACK_CSV,
                "batch_id": batch_id,
                "batch_token": token,
            },
        )
        self.assertEqual(ok.status_code, 200)

    def test_numeric_revision_credential_detects_import_only(self):
        self.client.post("/api/sample/load")  # import r1
        # Same-batch callback advances dataset revision to r2.
        self.client.post(
            "/api/callbacks",
            json={"answers": [{"household_id": "H006", "eligible": "是"}]},
        )
        # A form rendered at r2 stays valid with its revision floor.
        ok = self.client.post(
            "/callbacks",
            data={"updates_text": CALLBACK_CSV, "base_revision": "2"},
        )
        self.assertEqual(ok.status_code, 200)

        self.client.post("/api/sample/load")  # import r3
        stale = self.client.post(
            "/callbacks",
            data={"updates_text": CALLBACK_CSV, "base_revision": "2"},
        )
        self.assertEqual(stale.status_code, 409)

    def test_json_api_without_credential_is_unchanged(self):
        self.client.post("/api/sample/load")
        self.client.post("/api/sample/load")
        response = self.client.post(
            "/api/callbacks",
            json={"answers": [{"household_id": "H004", "eligible": "是"}]},
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["newly_filled"], 1)
        self.assertIn("revision", body)

        bad_token = self.client.post(
            "/api/callbacks",
            json={
                "answers": [{"household_id": "H005", "eligible": "否"}],
                "batch_id": "no-such-batch",
            },
        )
        self.assertEqual(bad_token.status_code, 409)
        self.assertTrue(bad_token.get_json()["stale_batch"])


class WaitingLockBatchTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="subsidy-lock-", suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        create_app({"DATABASE_PATH": self.db_path})
        submit_callbacks  # import ensures symbols used
        from meal_subsidy.services import import_csv

        import_csv(
            self.db_path,
            "户号,街道,是否有资格,是否领取\n1,A,,\n2,B,,\n",
            1,
        )

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def test_callback_waiting_on_lock_is_rejected_after_batch_switch(self):
        state_token = None
        with connect(self.db_path) as conn:
            state_token = conn.execute(
                "SELECT value FROM meta WHERE key='dataset_batch_id'"
            ).fetchone()[0]

        holder = connect(self.db_path)
        holder.execute("BEGIN IMMEDIATE")

        outcomes = []

        def worker():
            try:
                outcomes.append(
                    submit_callbacks(
                        self.db_path,
                        {"answers": [{"household_id": "1", "eligible": "是"}]},
                        expected_batch_id=state_token,
                    )
                )
            except BatchRejected as exc:
                outcomes.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        time.sleep(0.6)

        # The importer commits a new batch while the callback is queued.
        revision = bump_revision(holder, "import", 2, "concurrent reimport")
        set_meta(holder, "dataset_batch_revision", revision)
        set_meta(holder, "dataset_batch_id", uuid.uuid4().hex)
        holder.execute("COMMIT")
        holder.close()

        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], StaleBatchRejected)

        conn = connect(self.db_path)
        try:
            value = conn.execute(
                "SELECT eligible FROM households WHERE household_id='1'"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertIsNone(value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
