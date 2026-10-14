import unittest
from regression_verdict import evaluate


class VerdictTests(unittest.TestCase):
    def test_transport_ok_does_not_imply_approval(self):
        result = evaluate({'status': 'ok', 'resposta': 'tel:192', 'esperado': 'registrar glicemia'})
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['veredito'], 'inconclusivo')

    def test_forbidden_or_missing_content_fails(self):
        result = evaluate({'status': 'ok', 'resposta': 'tel:192', 'esperado': 'DEVE: /192 mg/\nNÃO DEVE: /tel:192/'})
        self.assertEqual(result['veredito'], 'reprovado')
        self.assertEqual(len(result['motivos']), 2)

    def test_full_card_has_testable_value_and_title_only_is_inconclusive(self):
        record = {'status': 'ok', 'resposta': 'Confirmação de Registro', 'checks': [{'pattern': '1500 ml', 'present': True}]}
        self.assertEqual(evaluate(record)['veredito'], 'inconclusivo')
        record['resposta'] += '\nAGUA: 1500 ml'
        self.assertEqual(evaluate(record)['veredito'], 'aprovado')

    def test_malformed_or_partial_expectation_never_passes(self):
        for expected in ('DEVE: /Resposta/\nconferir também o valor', 'DEVE: /[/'):
            self.assertEqual(evaluate({'status': 'ok', 'resposta': 'Resposta', 'esperado': expected})['veredito'], 'inconclusivo')
        self.assertEqual(evaluate({'status': 'sem-resposta'})['veredito'], 'reprovado')
        self.assertEqual(evaluate({'status': 'ok', 'resposta': ''})['veredito'], 'reprovado')
