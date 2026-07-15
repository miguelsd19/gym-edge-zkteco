import tempfile
import unittest
from pathlib import Path

from edge_service_zkteco.state_store import ProcessedEventStore


class ProcessedEventStoreTests(unittest.TestCase):
    def test_remember_and_contains_persist_across_instances(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"

            store = ProcessedEventStore(path)
            self.assertFalse(store.contains("evt-1"))
            store.remember("evt-1")
            self.assertTrue(store.contains("evt-1"))

            reloaded = ProcessedEventStore(path)
            self.assertTrue(reloaded.contains("evt-1"))
            self.assertFalse(reloaded.contains("evt-2"))

    def test_trims_to_max_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            store = ProcessedEventStore(path, max_keys=3)

            for index in range(5):
                store.remember(f"evt-{index}")

            self.assertFalse(store.contains("evt-0"))
            self.assertFalse(store.contains("evt-1"))
            self.assertTrue(store.contains("evt-2"))
            self.assertTrue(store.contains("evt-4"))

    def test_ignores_corrupt_state_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text("not-json", encoding="utf-8")

            store = ProcessedEventStore(path)

            self.assertFalse(store.contains("evt-1"))
            store.remember("evt-1")
            self.assertTrue(store.contains("evt-1"))
