"""Testes do rastreio (nunca quebra o produto, nunca vaza segredo)."""
import json
import os
import tempfile
import unittest

import trace_llm
from trace_llm import new_trace_id, trace_llm


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.jsonl")
        self._old = os.environ.get("TRACE_PATH")
        os.environ["TRACE_PATH"] = self.path
        self._old_en = os.environ.get("TRACE_LLM")
        os.environ["TRACE_LLM"] = "1"

    def tearDown(self):
        if self._old is None:
            os.environ.pop("TRACE_PATH", None)
        else:
            os.environ["TRACE_PATH"] = self._old
        if self._old_en is None:
            os.environ.pop("TRACE_LLM", None)
        else:
            os.environ["TRACE_LLM"] = self._old_en
        self.tmp.cleanup()

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    def test_roundtrip_with_id(self):
        tid = trace_llm("chat-ws", "request", user_text="oi")
        trace_llm("chat-ws", "router_hit", tid, action="CONVERSA")
        rows = self.read()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["trace_id"] == tid for r in rows))
        self.assertEqual(rows[0]["fields"]["user_text"], "oi")
        self.assertTrue(new_trace_id() != tid)

    def test_truncates_big_fields(self):
        os.environ["TRACE_CAP"] = "100"
        trace_llm("chat-rest", "speak_request", payload={"messages": [{"content": "x" * 500}]})
        rows = self.read()
        content = rows[0]["fields"]["payload"]["messages"][0]["content"]
        self.assertLess(len(content), 200)
        self.assertIn("truncado", content)
        os.environ.pop("TRACE_CAP", None)

    def test_disabled_writes_nothing(self):
        os.environ["TRACE_LLM"] = "0"
        tid = trace_llm("chat-ws", "request", user_text="oi")
        self.assertTrue(tid)
        self.assertFalse(os.path.exists(self.path))

    def test_file_mode_private(self):
        if os.name != "posix":
            self.skipTest("permissão POSIX só faz sentido no servidor Linux")
        trace_llm("chat-ws", "request")
        self.assertEqual(oct(os.stat(self.path).st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()
