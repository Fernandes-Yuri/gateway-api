"""Validação de fontes oficiais, unidades e preenchimento somente de nulos."""
import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from services import grocery_baseline as baseline
from services import price_cascade as cascade


def quote(name="CENOURA", unit="KG", classification="A", weight="1"):
    return {"produto": name, "classe": classification, "unidade": unit, "min": "9", "avg": "10", "max": "11",
            "peso_kg": weight, "grupo": "LEGUMES", "data": "2026-10-05", "url": baseline.URL}


class ParserTests(unittest.TestCase):
    def test_verified_columns_date_and_price_range(self):
        html = '<table class="contacao_lista"><tr><td>Categoria: LEGUMES Data: 05/10/2026</td></tr>'
        html += '<tr>' + ''.join('<td>'+x+'</td>' for x in
                               ['Produto', 'Classificação', 'Uni/Peso', 'Menor', 'Comum', 'Maior', 'Quilo']) + '</tr>'
        html += '<tr><td>CENOURA</td><td>A</td><td>KG</td><td>9,00</td><td>10,00</td><td>11,00</td><td>1</td></tr></table>'
        rows, errors = baseline.parse_table(html, "LEGUMES", "05/10/2026")
        self.assertEqual(rows[0]["avg"], "10.00")
        self.assertEqual(errors, 0)
        with self.assertRaises(ValueError):
            baseline.parse_table(html, "LEGUMES", "02/10/2026")
        with self.assertRaises(ValueError):
            baseline.parse_table(html.replace('Comum', 'Novo layout'), "LEGUMES", "05/10/2026")
        with self.assertRaises(ValueError):
            baseline.parse_table(html.replace('10,00', 'nan'), "LEGUMES", "05/10/2026")

    def test_procon_extracts_current_month_not_old_column(self):
        page = MagicMock()
        page.get_text.return_value = ('Variação Mensal do Custo Médio da Cesta Básica Agosto/26\n'
                                      'Feijão Carioquinha (kg) 8,96 8,63 -3,68%')
        document = MagicMock()
        document.__enter__.return_value = [page]
        with patch('pymupdf.open', return_value=document):
            rows = baseline.parse_procon(b'pdf', 'https://www.procon.sp.gov.br/report.pdf', datetime(2026, 10, 6))
        self.assertEqual(rows[0]['avg'], '8.63')
        self.assertEqual(rows[0]['data'], '2026-08-31')
        with patch('pymupdf.open', return_value=document), self.assertRaises(ValueError):
            baseline.parse_procon(b'pdf', 'url', datetime(2027, 1, 1))

    def test_box_conversion_and_retail_factor(self):
        items = [{"food_name": "alface", "search_term": "alface unidade"}]
        maps = [{"food_name": "alface", "ceagesp": ["ALFACE CRESPA"], "unidade": "UN", "fator_varejo": 2}]
        entries, missing = baseline.build_baseline(items, maps, [quote("ALFACE CRESPA", "ENG", "20 UNIDADES", "7")])
        self.assertEqual(entries[0]["price"], "1.00")
        self.assertEqual(missing, [])
        # Caixas sem contagem não podem virar unidades individuais.
        entries, missing = baseline.build_baseline(items, maps, [quote("ALFACE CRESPA", "ENG", "-", "7")])
        self.assertEqual(entries, [])
        self.assertEqual(missing, ['alface'])

    def test_package_weight_and_unknown_product(self):
        items = [{"food_name": "couve", "search_term": "couve 400g"}, {"food_name": "especial", "search_term": "xyz"}]
        maps = [{"food_name": "couve", "ceagesp": ["COUVE-DE-BRUXELAS"], "unidade": "KG", "quantidade": .4}]
        entries, missing = baseline.build_baseline(items, maps, [quote("COUVE-DE-BRUXELAS", "PCT", weight="0.25")])
        self.assertEqual(entries[0]['price'], '25.60')
        self.assertEqual(missing, ['especial'])

    def test_no_fuzzy_substitution(self):
        items = [{"food_name": "óleo abacate", "search_term": "oleo abacate"}]
        maps = [{"food_name": "óleo abacate", "ceagesp": ["OLEO DE ABACATE"], "unidade": "KG"}]
        self.assertEqual(baseline.build_baseline(items, maps, [quote("ABACATE")])[0], [])


class AutomaticTests(unittest.IsolatedAsyncioTestCase):
    async def test_fills_null_only_preserves_manual_and_existing_price(self):
        pool = AsyncMock()
        date = datetime(2026, 10, 5, tzinfo=timezone.utc)
        items = [{"food_name": name, "search_term": name} for name in ('novo', 'manual', 'existente')]
        pool.fetch.return_value = [
            {'food_name': 'novo', 'price_avg': None, 'source': 'cascade', 'updated_at': date},
            {'food_name': 'manual', 'price_avg': None, 'source': 'manual', 'updated_at': date},
            {'food_name': 'existente', 'price_avg': Decimal('9'), 'source': 'estimated', 'updated_at': date}]
        pool.fetchrow.return_value = {'food_name': 'novo'}
        entry = {'food_name': 'novo', 'price': '10.00', 'data': '2026-10-05', 'cotacoes': [quote()]}
        with patch.object(cascade._SEED_PATH.__class__, 'read_text', return_value=json.dumps(items)), \
                patch.object(cascade, 'fetch_baseline', new=AsyncMock(return_value=({'novo': entry}, 0))) as fetch, \
                patch.object(cascade, '_fetch_ipca', new=AsyncMock(return_value=({}, 0))), \
                patch.object(cascade, '_audit_baseline'):
            await cascade.preencher_baseline(pool)
        requested = fetch.call_args.args[1]
        self.assertEqual([item['food_name'] for item in requested], ['novo'])
        pool.fetchrow.assert_awaited_once()
        args = pool.fetchrow.call_args.args
        self.assertEqual(args[1], 'novo')
        self.assertEqual(args[6], 'estimated')
        self.assertEqual(args[9], date)
        self.assertIn('grocery_price_cache.price_avg IS NULL', args[0])

    async def test_source_failure_leaves_database_untouched(self):
        pool = AsyncMock()
        pool.fetch.return_value = []
        items = [{'food_name': 'sem fonte', 'search_term': 'xyz'}]
        with patch.object(cascade._SEED_PATH.__class__, 'read_text', return_value=json.dumps(items)), \
                patch.object(cascade, 'fetch_baseline', new=AsyncMock(return_value=({}, 1))), \
                patch.object(cascade, '_fetch_ipca', new=AsyncMock(return_value=({}, 0))):
            await cascade.preencher_baseline(pool)
        pool.fetchrow.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
