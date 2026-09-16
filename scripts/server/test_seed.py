"""Trava de coherência da semente (Fase 5): nada do que geramos pode ser quarentenável."""
import json
import os
import tempfile
import unittest

from curate_dataset import audit
from generate_fine_tuning_seed import generate_seed_dataset, SEED_CLASSIFY, SEED_SPEAK

LETHAL = ("false-confirmation", "diet-ghost")


class SeedTests(unittest.TestCase):
    def test_seed_content_sane(self):
        self.assertTrue(len(SEED_CLASSIFY) >= 10)
        self.assertTrue(len(SEED_SPEAK) >= 8)
        for _, fala, _ in SEED_SPEAK:
            self.assertNotIn("Já registrei", fala)
            self.assertNotIn("Já abri", fala)
            for tok in ("{", "}", "```", "💚", "*"):
                self.assertNotIn(tok, fala)

    def test_generated_seeds_pass_audit(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "seed.jsonl")
            generate_seed_dataset(out, append_to_existing=False)
            rows = [json.loads(l) for l in open(out, encoding="utf-8") if l.strip()]
            self.assertEqual(len(rows), len(SEED_CLASSIFY) + len(SEED_SPEAK))
            for r in rows:
                verdict, reasons = audit(r)
                lethal = [x for x in reasons if x.split(":")[0] in LETHAL]
                self.assertEqual(lethal, [], f"seed quarentenável: {r['messages'][1]['content'][:60]!r} -> {reasons}")
                self.assertEqual(r["metadata"]["format"], "two-step")


if __name__ == "__main__":
    unittest.main()
