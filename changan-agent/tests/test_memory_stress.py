import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from experiments.memory.stress import run_case


class MemoryStressTests(unittest.TestCase):
    def test_long_session_is_bounded_isolated_and_restartable(self):
        with tempfile.TemporaryDirectory() as folder:
            result = run_case(Path(folder), seed=7, snapshots=8)
        self.assertTrue(result["passed"], result["failures"])
        self.assertGreaterEqual(result["checks"], 50)

    def test_stress_seed_changes_only_synthetic_distractors(self):
        with tempfile.TemporaryDirectory() as folder:
            first = run_case(Path(folder) / "a", seed=7, snapshots=3)
            second = run_case(Path(folder) / "b", seed=23, snapshots=3)
        self.assertTrue(first["passed"], first["failures"])
        self.assertTrue(second["passed"], second["failures"])
        self.assertEqual(first["snapshots"], second["snapshots"])

    def test_missing_retrieval_is_reported_and_existing_data_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch("experiments.memory.stress.MemoryStore.search", return_value=[]):
                result = run_case(Path(folder), seed=7, snapshots=1)
            self.assertFalse(result["passed"])
            self.assertTrue(any("最新发布日期" in item for item in result["failures"]))
            with self.assertRaises(FileExistsError):
                run_case(Path(folder), seed=7, snapshots=1)
