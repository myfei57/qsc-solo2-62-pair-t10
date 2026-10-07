"""Segmented store persistence, crash recovery and ops endpoints."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from coldstore.console.seed import seed_defaults
from coldstore.console.server import Server
from coldstore.store import SegmentedStore, Store, segment_for


class SegmentMappingCase(unittest.TestCase):
    def test_prefix_determines_segment(self) -> None:
        self.assertEqual(segment_for("temp:last"), "temp")
        self.assertEqual(segment_for("valve:supply:c1"), "valve")
        self.assertEqual(segment_for("plain"), "core")


class SegmentedStoreCase(unittest.TestCase):
    def open_store(self, tmp: str) -> SegmentedStore:
        return SegmentedStore.open(f"{tmp}/state.json")

    def test_writes_split_into_segment_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.open_store(tmp)
            store.put("temp:last", "-18.0")
            store.put("ammonia:threshold", "25.0")
            store.put("note", "free text")
            segdir = f"{tmp}/state.segments"
            self.assertTrue(os.path.isfile(f"{segdir}/temp.seg.json"))
            self.assertTrue(os.path.isfile(f"{segdir}/ammonia.seg.json"))
            self.assertTrue(os.path.isfile(f"{segdir}/core.seg.json"))
            self.assertEqual(store.keys(), ["ammonia:threshold", "note", "temp:last"])
            reopened = self.open_store(tmp)
            self.assertEqual(reopened.get("temp:last"), ("-18.0", True))
            self.assertEqual(reopened.get("ammonia:threshold"), ("25.0", True))
            self.assertEqual(reopened.get("note"), ("free text", True))

    def test_versions_advance_per_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.open_store(tmp)
            store.put("temp:last", "-18.0")
            store.put("temp:last", "-17.0")
            store.put("ammonia:last", "3.0")
            health = {row.name: row for row in store.segment_health()}
            self.assertEqual(health["temp"].version, 2)
            self.assertEqual(health["ammonia"].version, 1)
            self.assertEqual(health["temp"].status, "ok")
            self.assertTrue(health["temp"].checksum.startswith("sha256:"))

    def test_legacy_document_migrates_into_segments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Store.open(f"{tmp}/state.json")
            legacy.put("temp:last", "-18.0")
            legacy.put("interlock:latch", "true")
            legacy.close()
            store = self.open_store(tmp)
            self.assertEqual(store.get("temp:last"), ("-18.0", True))
            self.assertEqual(store.get("interlock:latch"), ("true", True))
            self.assertFalse(os.path.exists(f"{tmp}/state.json"))
            self.assertTrue(os.path.isfile(f"{tmp}/state.json.migrated"))
            records = store.recovery_records()
            self.assertEqual(
                sorted(record.segment for record in records if record.action == "migrate"),
                ["interlock", "temp"],
            )

    def test_corrupt_legacy_document_is_preserved_and_reset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with open(f"{tmp}/state.json", "w", encoding="utf-8") as handle:
                handle.write("{not json")
            store = self.open_store(tmp)
            self.assertEqual(store.count(), 0)
            self.assertTrue(os.path.isfile(f"{tmp}/state.json.corrupt"))
            self.assertIn("migrate-failed", [record.action for record in store.recovery_records()])

    def test_corrupt_segment_rolls_back_to_previous_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.open_store(tmp)
            store.put("temp:last", "-18.0")
            store.put("temp:last", "-17.5")
            store.put("ammonia:last", "3.0")
            with open(f"{tmp}/state.segments/temp.seg.json", "w", encoding="utf-8") as handle:
                handle.write("garbage{{{")
            reopened = self.open_store(tmp)
            self.assertEqual(reopened.get("temp:last"), ("-18.0", True))
            self.assertEqual(reopened.get("ammonia:last"), ("3.0", True))
            health = {row.name: row for row in reopened.segment_health()}
            self.assertEqual(health["temp"].status, "recovered")
            self.assertEqual(health["temp"].version, 1)
            self.assertEqual(health["ammonia"].status, "ok")
            rollbacks = [r for r in reopened.recovery_records() if r.action == "rollback"]
            self.assertEqual(len(rollbacks), 1)
            self.assertEqual(rollbacks[0].segment, "temp")
            self.assertEqual(rollbacks[0].to_version, 1)

    def test_segment_and_backup_corrupt_resets_only_that_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.open_store(tmp)
            store.put("temp:last", "-18.0")
            store.put("ammonia:last", "3.0")
            for suffix in ("", ".bak"):
                with open(
                    f"{tmp}/state.segments/temp.seg.json{suffix}", "w", encoding="utf-8"
                ) as handle:
                    handle.write("junk")
            reopened = self.open_store(tmp)
            self.assertEqual(reopened.get("temp:last"), ("", False))
            self.assertEqual(reopened.get("ammonia:last"), ("3.0", True))
            health = {row.name: row for row in reopened.segment_health()}
            self.assertEqual(health["temp"].status, "degraded")
            self.assertEqual(health["ammonia"].status, "ok")
            self.assertIn("reset", [record.action for record in reopened.recovery_records()])
            reopened.put("temp:last", "-19.0")
            self.assertEqual(self.open_store(tmp).get("temp:last"), ("-19.0", True))

    def test_unconfirmed_staging_file_is_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.open_store(tmp)
            store.put("temp:last", "-18.0")
            with open(
                f"{tmp}/state.segments/temp.seg.json.tmp", "w", encoding="utf-8"
            ) as handle:
                handle.write("partial")
            reopened = self.open_store(tmp)
            self.assertEqual(reopened.get("temp:last"), ("-18.0", True))
            self.assertFalse(os.path.exists(f"{tmp}/state.segments/temp.seg.json.tmp"))
            self.assertIn("orphan-tmp", [record.action for record in reopened.recovery_records()])

    def test_manifest_rebuilt_when_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.open_store(tmp)
            store.put("temp:last", "-18.0")
            with open(f"{tmp}/state.segments/manifest.json", "w", encoding="utf-8") as handle:
                handle.write("junk")
            reopened = self.open_store(tmp)
            self.assertEqual(reopened.get("temp:last"), ("-18.0", True))
            self.assertIn(
                "manifest-rebuild", [record.action for record in reopened.recovery_records()]
            )

    def test_clear_removes_all_segments(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.open_store(tmp)
            store.put("temp:last", "-18.0")
            store.put("ammonia:last", "3.0")
            store.clear()
            self.assertEqual(store.count(), 0)
            leftover = [
                entry for entry in os.listdir(f"{tmp}/state.segments") if ".seg.json" in entry
            ]
            self.assertEqual(leftover, [])
            self.assertIn("clear", [record.action for record in store.recovery_records()])
            store.put("temp:last", "-20.0")
            self.assertEqual(self.open_store(tmp).get("temp:last"), ("-20.0", True))


class SegmentConsoleCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SegmentedStore.open(f"{self._tmp.name}/state.json")
        seed_defaults(self.store)
        self.server = Server(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None):
        response = self.server.dispatch(method, path, payload)
        body = response.body.decode("utf-8")
        parsed = json.loads(body) if response.content_type.startswith("application/json") else body
        return response.status, parsed

    def test_segments_endpoint_lists_health(self) -> None:
        status, body = self.call("GET", "/store/segments")
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "segmented")
        names = {row["name"] for row in body["segments"]}
        self.assertIn("temp", names)
        self.assertIn("ammonia", names)
        self.assertIn("console", names)
        self.assertTrue(all(row["status"] == "ok" for row in body["segments"]))
        self.assertTrue(all(row["version"] >= 1 for row in body["segments"]))
        self.assertEqual(body["degraded"], [])
        self.assertEqual(body["recovered"], [])

    def test_recovery_endpoint_and_degraded_segment_surface(self) -> None:
        status, body = self.call("GET", "/store/recovery")
        self.assertEqual(status, 200)
        self.assertEqual(body["records"], [])

        self.store.close()
        segfile = f"{self._tmp.name}/state.segments/temp.seg.json"
        for suffix in ("", ".bak"):
            with open(f"{segfile}{suffix}", "w", encoding="utf-8") as handle:
                handle.write("junk")
        self.store = SegmentedStore.open(f"{self._tmp.name}/state.json")
        self.server = Server(self.store)

        status, body = self.call("GET", "/store/recovery")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)
        record = body["records"][0]
        self.assertEqual(record["action"], "reset")
        self.assertEqual(record["segment"], "temp")
        self.assertGreater(record["at"], 0)

        status, body = self.call("GET", "/store/segments")
        statuses = {row["name"]: row["status"] for row in body["segments"]}
        self.assertEqual(statuses["temp"], "degraded")
        self.assertEqual(statuses["ammonia"], "ok")
        self.assertEqual(body["degraded"], ["temp"])

        status, checks = self.call("GET", "/health/checks")
        store_row = [row for row in checks["checks"] if row["name"] == "store"][0]
        self.assertEqual(store_row["status"], "fail")
        self.assertIn("temp", store_row["detail"])

    def test_control_writes_persist_across_reopen(self) -> None:
        status, _ = self.call("POST", "/temp/read", {"value": -18.0})
        self.assertEqual(status, 200)
        status, _ = self.call("POST", "/interlock/trip", {"reason": "ammonia high"})
        self.assertEqual(status, 200)
        self.store.close()
        self.store = SegmentedStore.open(f"{self._tmp.name}/state.json")
        self.server = Server(self.store)
        status, body = self.call("GET", "/temp")
        self.assertEqual(body["value"], -18.0)
        status, body = self.call("GET", "/interlock")
        self.assertTrue(body["tripped"])
        self.assertEqual(body["reason"], "ammonia high")


class LegacyConsoleCase(unittest.TestCase):
    def test_segment_endpoints_degrade_for_single_document_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            seed_defaults(store)
            server = Server(store)
            response = server.dispatch("GET", "/store/segments")
            body = json.loads(response.body)
            self.assertEqual(body["mode"], "legacy")
            self.assertEqual(body["count"], 1)
            self.assertEqual(body["segments"][0]["name"], "legacy")
            self.assertEqual(body["segments"][0]["status"], "ok")
            response = server.dispatch("GET", "/store/recovery")
            self.assertEqual(json.loads(response.body)["records"], [])
            store.close()


if __name__ == "__main__":
    unittest.main()
