import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from export_vip_firestore import write_export


class ExportTests(unittest.TestCase):
    def test_keeps_invalid_records_and_escapes_copy_data(self):
        docs = [SimpleNamespace(id='one', to_dict=lambda: {'fullName': "Ana \\ O'Brien", 'email': 'ANA@EXAMPLE.COM'}),
                SimpleNamespace(id='two', to_dict=lambda: {'email': 'bad'})]
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'export'
            self.assertEqual((2,1,1), write_export(docs, output))
            self.assertEqual(2,len(json.loads((output/'firestore-vip-backup.json').read_text(encoding='utf-8'))))
            self.assertEqual(1,len(json.loads((output/'revisar-invalidos.json').read_text(encoding='utf-8'))))
            sql = (output/'import.sql').read_text(encoding='utf-8')
            self.assertIn('ON CONFLICT(email) DO NOTHING', sql)
            self.assertIn('ana@example.com', sql)
            # Decode PostgreSQL COPY backslash escapes, then decode the JSON document.
            row = next(line for line in sql.splitlines() if line.startswith('{'))
            restored = json.loads(row.replace('\\\\', '\\'))
            self.assertEqual("Ana \\ O'Brien", restored['fullName'])
            with self.assertRaises(FileExistsError): write_export([], output)
