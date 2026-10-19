import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

_spec = importlib.util.spec_from_file_location('retired_files', Path(__file__).resolve().parents[1]/'purge_retired_channel_files.py')
retired = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(retired)


class RetiredFilesTests(unittest.TestCase):
    def test_mixed_files_keep_app_records_and_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict('os.environ', {}, clear=True):
            root = Path(temp)
            data = root/'dataset_braga.jsonl'
            app = json.dumps({'source':'app_chat','messages':['synthetic app']})+'\n'
            data.write_text(app+json.dumps({'source':'whatsapp_voice','messages':['synthetic retired']})+'\n')
            (root/'webhook_outbox.log').write_text('synthetic retired outbox')
            cache = root/'app/__pycache__'
            cache.mkdir(parents=True)
            (cache/'whatsapp_service.cpython-312.pyc').write_bytes(b'synthetic')
            self.assertEqual(retired.purge(root), 1)
            self.assertEqual(data.read_text(), app)
            self.assertFalse((root/'webhook_outbox.log').exists())
            self.assertFalse(cache.exists())
            self.assertEqual(retired.purge(root), 0)

    def test_completed_retirement_does_not_delete_a_future_channel(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict('os.environ', {}, clear=True):
            root = Path(temp)
            retired.purge(root)
            data = root/'dataset_braga.jsonl'
            future = '{"source":"whatsapp","purpose":"new service"}\n'
            data.write_text(future)
            self.assertEqual(retired.purge(root), 0)
            self.assertEqual(data.read_text(), future)

    def test_invalid_input_keeps_original_and_does_not_report_success(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'trace.jsonl'
            original = '{"channel":"whatsapp"}\nINVALID\n'
            path.write_text(original)
            with self.assertRaises(ValueError): retired.rewrite(path)
            self.assertEqual(path.read_text(), original)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_symbolic_link_cannot_rewrite_another_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); target = root/'original'; target.write_text('{"channel":"whatsapp"}\n')
            link = root/'link'; link.symlink_to(target)
            with self.assertRaises(ValueError): retired.rewrite(link)
            self.assertTrue(target.exists())
