"""Build the attributed TACO snapshot from NEPA/UNICAMP's official spreadsheet.

Generation only: python -m pip install openpyxl; python scripts/build_taco_reference.py workbook.xlsx
Runtime uses the resulting JSON, with no spreadsheet dependency or API credentials.
"""
import argparse
import hashlib
import json
from pathlib import Path

SOURCE_URL = 'https://www.nepa.unicamp.br/wp-content/uploads/sites/27/2023/10/Taco-4a-Edicao.xlsx'
SOURCE_PAGE = 'https://nepa.unicamp.br/publicacoes/'
FIELDS = [(3, 'calories', 'Calorias', 'kcal'), (8, 'carbohydrates', 'Carboidratos', 'g'),
          (5, 'protein', 'Proteínas', 'g'), (6, 'fat', 'Gorduras', 'g'),
          (9, 'fiber', 'Fibras alimentares', 'g'), (17, 'sodium', 'Sódio', 'mg'),
          (7, 'cholesterol', 'Colesterol', 'mg'), (11, 'calcium', 'Cálcio', 'mg'),
          (16, 'iron', 'Ferro', 'mg'), (18, 'potassium', 'Potássio', 'mg'),
          (12, 'magnesium', 'Magnésio', 'mg'), (15, 'phosphorus', 'Fósforo', 'mg'),
          (20, 'zinc', 'Zinco', 'mg'), (28, 'vitamin_c', 'Vitamina C', 'mg')]
FATS = [(2, 'saturated_fat', 'Gorduras saturadas', 'g'),
        (3, 'monounsaturated_fat', 'Gorduras monoinsaturadas', 'g'),
        (4, 'polyunsaturated_fat', 'Gorduras poli-insaturadas', 'g')]


def cell_fact(value, nutrient_key, label, unit):
    if isinstance(value, (float, int)) and not isinstance(value, bool) and value >= 0:
        return {'key': nutrient_key, 'label': label, 'unit': unit, 'value': round(value, 6)}
    status = 'trace' if str(value).strip().lower() == 'tr' else 'not_applicable' if str(value).strip().lower() == 'na' else 'not_reported'
    return {'key': nutrient_key, 'label': label, 'unit': unit, 'status': status}


def build(path):
    from openpyxl import load_workbook
    workbook = load_workbook(path, read_only=True, data_only=True)
    fatty_acids = {int(row[0]): row for row in workbook.worksheets[1].values
                   if isinstance(row[0], (float, int)) and isinstance(row[1], str)}
    foods, category = [], ''
    for row in workbook.worksheets[0].values:
        if isinstance(row[0], str) and not row[1] and row[0] not in ('Número do', 'Alimento'):
            category = row[0].strip()
        if not isinstance(row[0], (float, int)) or not isinstance(row[1], str):
            continue
        food_id = int(row[0])
        facts = [cell_fact(row[i], key, label, unit) for i, key, label, unit in FIELDS]
        if food_id in fatty_acids:
            fats = fatty_acids[food_id]
            facts += [cell_fact(fats[i], key, label, unit) for i, key, label, unit in FATS]
        foods.append({'id': f'TACO:{food_id}', 'name': row[1].strip(), 'category': category,
                      'basis_g': 100, 'nutrients': facts})
    return {'source': {'name': 'TACO / NEPA-UNICAMP', 'version': '4ª edição, 2011',
                       'url': SOURCE_PAGE, 'download_url': SOURCE_URL,
                       'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                       'attribution': 'Tabela Brasileira de Composição de Alimentos. NEPA-UNICAMP, 4ª edição revisada e ampliada. Campinas, 2011.',
                       'reuse': 'Reprodução permitida mediante citação da fonte, conforme publicação oficial.'},
            'foods': foods}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('workbook', type=Path)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent / 'server/data/nutrition_reference/taco.json')
    args = parser.parse_args()
    dataset = build(args.workbook)
    args.output.write_text(json.dumps(dataset, ensure_ascii=False, separators=(',', ':')) + '\n')
    print(f"{len(dataset['foods'])} referências TACO exportadas")
