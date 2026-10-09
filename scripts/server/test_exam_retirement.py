"""O expurgo alcança só a área retirada, incluindo links sem seguir destinos."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "purge_legacy_exam_files", Path(__file__).resolve().parents[1] / "purge_legacy_exam_files.py"
)
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class ExamRetirementTest(unittest.TestCase):
    def test_purge_is_idempotent_and_preserves_prescriptions(self):
        with tempfile.TemporaryDirectory() as root:
            storage = Path(root)
            (storage / "exams/u").mkdir(parents=True)
            (storage / "exams/u/file.pdf").write_bytes(b"legacy")
            (storage / "prescriptions").mkdir()
            prescription = storage / "prescriptions/file.pdf"
            prescription.write_bytes(b"preserve")
            cleanup.purge(storage)
            cleanup.purge(storage)
            self.assertFalse((storage / "exams").exists())
            self.assertEqual(prescription.read_bytes(), b"preserve")

    def test_symlink_is_unlinked_without_removing_destination(self):
        with tempfile.TemporaryDirectory() as root:
            storage = Path(root)
            target = storage / "outside"
            target.mkdir()
            original = target / "original.pdf"
            original.write_bytes(b"preserve")
            try:
                (storage / "exams").symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest("Links simbólicos indisponíveis neste dispositivo")
            cleanup.purge(storage)
            self.assertEqual(original.read_bytes(), b"preserve")
