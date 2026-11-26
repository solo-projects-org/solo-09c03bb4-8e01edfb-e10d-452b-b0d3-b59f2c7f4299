from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from meal_subsidy import create_app  # noqa: E402
from meal_subsidy.services import BatchRejected, import_csv, submit_callbacks  # noqa: E402


class WebFlowTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="subsidy-test-", suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)  # let SQLite create a clean file
        self.app = create_app({"DATABASE_PATH": self.db_path, "TESTING": True})
        self.client = self.app.test_client()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def test_health_and_empty_home(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ok")
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn("导入逐户".encode(), home.data)

    def test_sample_compare_and_zero_warning(self):
        load = self.client.post("/api/sample/load")
        self.assertEqual(load.status_code, 200)
        self.assertTrue(load.get_json()["ok"])

        api = self.client.get("/api/compare?street1=人民路街道&street2=建设路街道")
        self.assertEqual(api.status_code, 200)
        data = api.get_json()
        self.assertEqual(data["min"]["text"], "-7/15")
        self.assertEqual(data["max"]["text"], "4/5")
        self.assertEqual(data["revision"], 1)

        html = self.client.get("/compare?street1=人民路街道&street2=湖滨街道")
        self.assertEqual(html.status_code, 200)
        self.assertIn("zero-warning", html.text)
        self.assertIn("min-value", html.text)
        self.assertIn("max-value", html.text)

    def test_callback_batch_is_idempotent(self):
        self.client.post("/api/sample/load")
        text = (
            "户号,是否有资格,是否领取\n"
            "H004,是,\n"
            "H005,否,\n"
            "H102,是,是\n"
        )
        first = self.client.post("/callbacks", data={"updates_text": text})
        self.assertEqual(first.status_code, 200)
        self.assertIn('data-testid="newly-filled">4<', first.text)

        second = self.client.post("/api/callbacks", json={
            "answers": [
                {"household_id": "H004", "eligible": "是"},
                {"household_id": "H005", "eligible": "否"},
                {"household_id": "H102", "eligible": "是", "received": "是"},
            ]
        })
        body = second.get_json()
        self.assertEqual(second.status_code, 200)
        self.assertEqual(body["newly_filled"], 0)
        self.assertEqual(body["unchanged_answers"], 4)

    def test_conflicting_callback_and_import_leave_data_intact(self):
        self.client.post("/api/sample/load")
        conflict = self.client.post("/api/callbacks", json={
            "answers": [{"household_id": "H001", "eligible": "否"}]
        })
        self.assertEqual(conflict.status_code, 400)
        households = self.client.get("/api/households").get_json()["households"]
        h001 = next(row for row in households if row["household_id"] == "H001")
        self.assertEqual(h001["eligible"], 1)

        atomic = self.client.post("/api/callbacks", json={
            "answers": [
                {"household_id": "H003", "received": "是"},
                {"household_id": "H001", "eligible": "否"},
            ]
        })
        self.assertEqual(atomic.status_code, 400)
        households = self.client.get("/api/households").get_json()["households"]
        h003 = next(row for row in households if row["household_id"] == "H003")
        self.assertIsNone(h003["received"])

        bad_csv = "户号,街道,是否有资格,是否领取\nX1,测试街,是,是\n"
        bad_import = self.client.post("/api/import", json={
            "csv": bad_csv,
            "district_total": 999,
        })
        self.assertEqual(bad_import.status_code, 400)
        state = self.client.get("/api/state").get_json()
        self.assertEqual(state["revision"], 1)
        self.assertEqual(state["household_count"], 16)

    def test_multipart_import_supports_english_headers(self):
        import io

        csv_text = (
            "household_id,street,eligible,received\n"
            "1,North,yes,yes\n"
            "2,North,no,no\n"
            "3,South,,\n"
        ).encode()
        response = self.client.post(
            "/import",
            data={
                "district_total": "2",
                "file": (io.BytesIO(csv_text), "x.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        compare = self.client.get("/api/compare?street1=North&street2=South")
        self.assertEqual(compare.status_code, 200)
        self.assertTrue(compare.get_json()["positive_pair_exists"])


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(prefix="subsidy-conc-", suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)
        create_app({"DATABASE_PATH": self.db_path})
        import_csv(
            self.db_path,
            "户号,街道,是否有资格,是否领取\n1,A,,\n2,B,,\n3,C,,\n",
            1,
        )

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except FileNotFoundError:
                pass

    def test_concurrent_batches_are_checked_against_committed_answers(self):
        for _ in range(15):
            # Reset to revision/initial data for every trial.
            import_csv(
                self.db_path,
                "户号,街道,是否有资格,是否领取\n1,A,,\n2,B,,\n3,C,,\n",
                1,
            )
            barrier = threading.Barrier(2)
            outcomes = []

            def worker(household_id):
                barrier.wait()
                try:
                    outcomes.append(submit_callbacks(
                        self.db_path,
                        {"answers": [{"household_id": household_id, "eligible": "是"}]},
                    ))
                except BatchRejected as exc:
                    outcomes.append(exc)

            threads = [threading.Thread(target=worker, args=(hid,)) for hid in ("1", "2")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            committed = [x for x in outcomes if isinstance(x, dict)]
            rejected = [x for x in outcomes if isinstance(x, BatchRejected)]
            self.assertEqual(len(committed), 1, outcomes)
            self.assertEqual(len(rejected), 1, outcomes)

            import sqlite3
            conn = sqlite3.connect(self.db_path)
            try:
                eligible_yes = conn.execute(
                    "SELECT COUNT(*) FROM households WHERE eligible=1"
                ).fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(eligible_yes, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
