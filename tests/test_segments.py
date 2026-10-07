"""Crash-consistency tests for the segmented store."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from coldstore.console.seed import seed_defaults
from coldstore.console.server import Server
from coldstore.segments import SegmentedStore, import_legacy, segment_for
from coldstore.segments.recovery import manifest_path, segments_dir
from coldstore.segments.segment import encode_segment, segment_file_name
from coldstore.store import Store


def corrupt(path: str) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("{not json")


def segment_path(root: str, name: str, version: int) -> str:
    return os.path.join(segments_dir(root), segment_file_name(name, version))


class SegmentedStoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = f"{self._tmp.name}/state"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def open(self) -> SegmentedStore:
        return SegmentedStore.open(self.root)

    def test_round_trip_persists_and_counts(self) -> None:
        store = self.open()
        store.put("temp:last", "1.5")
        store.put("ammonia:threshold", "25.0")
        self.assertEqual(store.get("temp:last"), ("1.5", True))
        self.assertEqual(store.get("missing"), ("", False))
        self.assertEqual(store.count(), 2)
        store.delete("temp:last")
        self.assertEqual(store.keys(), ["ammonia:threshold"])
        reopened = self.open()
        self.assertEqual(reopened.get("ammonia:threshold"), ("25.0", True))
        store.clear()
        self.assertEqual(store.count(), 0)

    def test_keys_route_to_subsystem_segments(self) -> None:
        store = self.open()
        store.put("temp:last", "1.5")
        store.put("interlock:reason", "manual")
        store.put("unregistered-key", "x")
        self.assertEqual(segment_for("temp:last"), "temp")
        self.assertEqual(segment_for("unregistered-key"), "shared")
        files = os.listdir(segments_dir(self.root))
        self.assertIn("temp.v1.json", files)
        self.assertIn("interlock.v1.json", files)
        self.assertIn("shared.v1.json", files)
        self.assertNotIn("ammonia.v1.json", files)

    def test_unconfirmed_segment_discarded_after_crash(self) -> None:
        store = self.open()
        store.put("temp:last", "1.0")
        store.close()
        # Simulate a crash between landing v2 and confirming it: the segment
        # file exists but the manifest still points at v1.
        orphan = encode_segment("temp", 2, {"temp:last": "2.0"})
        with open(segment_path(self.root, "temp", 2), "w", encoding="utf-8") as handle:
            handle.write(orphan)
        recovered = self.open()
        self.assertEqual(recovered.get("temp:last"), ("1.0", True))
        self.assertFalse(os.path.exists(segment_path(self.root, "temp", 2)))
        discards = [r for r in recovered.journal_records() if r.op == "discard"]
        self.assertEqual([(r.segment, r.version) for r in discards], [("temp", 2)])

    def test_corrupt_segment_rolls_back_whole_and_alone(self) -> None:
        store = self.open()
        store.put("temp:last", "1.0")
        store.put("temp:last", "2.0")
        store.put("ammonia:threshold", "30.0")
        store.close()
        corrupt(segment_path(self.root, "temp", 2))
        recovered = self.open()
        # Whole-segment replacement: temp falls back to v1 as a unit.
        self.assertEqual(recovered.get("temp:last"), ("1.0", True))
        # The neighbour segment is untouched.
        self.assertEqual(recovered.get("ammonia:threshold"), ("30.0", True))
        health = {h.name: h for h in recovered.segment_health()}
        self.assertEqual(health["temp"].status, "recovered")
        self.assertEqual(health["temp"].version, 1)
        self.assertEqual(health["ammonia"].status, "ok")
        rollbacks = [r for r in recovered.journal_records() if r.op == "rollback"]
        self.assertEqual([(r.segment, r.version) for r in rollbacks], [("temp", 1)])
        # Version ids stay monotonic after a rollback.
        recovered.put("temp:last", "3.0")
        self.assertTrue(os.path.exists(segment_path(self.root, "temp", 3)))

    def test_segment_reset_when_every_version_corrupt(self) -> None:
        store = self.open()
        store.put("quota:runtime-accumulator", "7.5")
        store.put("temp:last", "1.0")
        store.close()
        corrupt(segment_path(self.root, "quota", 1))
        recovered = self.open()
        self.assertEqual(recovered.get("quota:runtime-accumulator"), ("", False))
        self.assertEqual(recovered.get("temp:last"), ("1.0", True))
        health = {h.name: h for h in recovered.segment_health()}
        self.assertEqual(health["quota"].status, "degraded")
        self.assertEqual(health["temp"].status, "ok")
        resets = [r for r in recovered.journal_records() if r.op == "reset"]
        self.assertEqual([r.segment for r in resets], ["quota"])

    def test_manifest_rebuilt_when_corrupt(self) -> None:
        store = self.open()
        store.put("temp:last", "1.0")
        store.put("evap:baseline", "12.0")
        store.close()
        corrupt(manifest_path(self.root))
        recovered = self.open()
        self.assertEqual(recovered.get("temp:last"), ("1.0", True))
        self.assertEqual(recovered.get("evap:baseline"), ("12.0", True))
        rebuilds = [r for r in recovered.journal_records() if r.op == "rebuild"]
        self.assertEqual(len(rebuilds), 1)

    def test_journal_records_commits_and_clean_shutdown(self) -> None:
        store = self.open()
        store.put("temp:last", "1.0")
        store.close()
        records = self.open().journal_records()
        commits = [r for r in records if r.op == "commit"]
        self.assertIn(("temp", 1), [(r.segment, r.version) for r in commits])
        self.assertEqual(records[-1].op, "open")
        self.assertIn("close", [r.op for r in records])

    def test_bulk_load_groups_by_segment(self) -> None:
        store = self.open()
        counts = store.bulk_load({"temp:last": "1.0", "temp:readings": "[]", "quota:x": "2"})
        self.assertEqual(counts, {"temp": 2, "quota": 1})
        self.assertEqual(self.open().get("quota:x"), ("2", True))

    def test_migrate_imports_legacy_store(self) -> None:
        legacy = f"{self._tmp.name}/state.json"
        with open(legacy, "w", encoding="utf-8") as handle:
            json.dump({"temp:last": "1.0", "interlock:latch": "true"}, handle)
        store = self.open()
        result = import_legacy(store, legacy)
        self.assertEqual(result["imported"], 2)
        self.assertFalse(os.path.exists(legacy))
        self.assertTrue(os.path.exists(f"{legacy}.migrated"))
        self.assertEqual(store.get("interlock:latch"), ("true", True))
        migrates = [r for r in store.journal_records() if r.op == "migrate"]
        self.assertEqual(len(migrates), 1)


class ConsoleSegmentsCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = SegmentedStore.open(f"{self._tmp.name}/state")
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
        status, payload = self.call("GET", "/segments")
        self.assertEqual(status, 200)
        self.assertEqual(payload["mode"], "segmented")
        self.assertTrue(payload["ok"])
        names = {segment["name"] for segment in payload["segments"]}
        self.assertIn("temp", names)
        self.assertIn("interlock", names)
        seeded = {s["name"]: s for s in payload["segments"]}
        self.assertGreater(seeded["temp"]["keys"], 0)

    def test_segments_journal_endpoint(self) -> None:
        self.call("POST", "/interlock/trip", {"reason": "test"})
        status, payload = self.call("GET", "/segments/journal")
        self.assertEqual(status, 200)
        ops = [record["op"] for record in payload["records"]]
        self.assertIn("commit", ops)

    def test_ops_segments_page(self) -> None:
        status, page = self.call("GET", "/ops/segments")
        self.assertEqual(status, 200)
        self.assertIn("Segment health", page)
        self.assertIn("interlock", page)
        self.assertIn("OK", page)

    def test_segmented_store_survives_console_writes(self) -> None:
        self.call("POST", "/ammonia/threshold", {"threshold": 40.0})
        self.call("POST", "/interlock/trip", {"reason": "test"})
        reopened = SegmentedStore.open(self.store.path)
        self.assertEqual(reopened.get("ammonia:threshold"), ("40.0000", True))
        self.assertEqual(reopened.get("interlock:reason"), ("test", True))


class LegacyModeCase(unittest.TestCase):
    def test_segments_endpoint_reports_legacy_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store.open(f"{tmp}/state.json")
            server = Server(store)
            response = server.dispatch("GET", "/segments")
            payload = json.loads(response.body.decode("utf-8"))
            self.assertEqual(payload["mode"], "legacy")
            self.assertEqual(payload["segments"], [])
            response = server.dispatch("GET", "/ops/segments")
            self.assertIn("single-file store", response.body.decode("utf-8"))
            store.close()


if __name__ == "__main__":
    unittest.main()
