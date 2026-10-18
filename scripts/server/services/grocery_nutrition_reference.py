"""Attributed composition references and explicit, reviewable package conversions."""
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata

DATA = Path(__file__).resolve().parents[1] / 'data/nutrition_reference/taco.json'
STOPWORDS = {'de', 'do', 'da', 'dos', 'das', 'e', 'com', 'sem', 'em', 'ao', 'a', 'o'}
DENSITIES = {'TACO:272': {'value': Decimal('0.92'),
                        'source': 'Aproximação de densidade para óleo de soja, baseada na faixa Codex/FAO a 20 °C',
                        'url': 'https://www.fao.org/4/Y2774E/y2774e04.htm'}}


@lru_cache(maxsize=1)
def dataset():
    return json.loads(DATA.read_text())


def tokens(name):
    normalized = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().lower()
    return {'ovo' if word == 'ovos' else word
            for word in re.findall(r'[a-z0-9]+', normalized) if word not in STOPWORDS}


def is_retail_reference(food):
    """Keep market ingredients/products, excluding culinary preparations."""
    if food['category'] == 'Alimentos preparados':
        return False
    # Industrial chips are sold fried; do not confuse manufacture with home cooking.
    words = tokens(food['name'])
    if 'industrializada' in words or 'industrializado' in words:
        return True
    cooking = {'cozido', 'cozida', 'cozidos', 'cozidas', 'frito', 'frita', 'fritos',
               'fritas', 'assado', 'assada', 'assados', 'assadas', 'grelhado',
               'grelhada', 'refogado', 'refogada'}
    if words & cooking:
        return False
    if food['category'] in {'Gorduras e óleos', 'Leite e derivados', 'Outros alimentos industrializados'}:
        return True
    return not re.search(r'\bcom (?:manteiga|oleo|azeite|leite)\b',
                         unicodedata.normalize('NFKD', food['name']).encode('ascii', 'ignore').decode().lower())


def preview(food, amount, unit, total_grams=None):
    amount = Decimal(str(amount))
    if not amount.is_finite() or not 0 < amount <= 1000000 or unit not in ('g', 'ml', 'un'):
        raise ValueError('Informe o conteúdo e a unidade da embalagem.')
    if unit == 'un' and amount != amount.to_integral_value():
        raise ValueError('O conteúdo em unidades precisa ser inteiro.')
    conversion = {'package_amount': float(amount), 'package_unit': unit, 'total_grams': None,
                  'density_g_per_ml': None, 'density_source': None, 'density_url': None,
                  'requires_weight': False, 'method': 'mass'}
    if unit == 'g':
        mass = amount
    elif total_grams is not None:
        mass = Decimal(str(total_grams))
        if not mass.is_finite() or not 0 < mass <= 10000000:
            raise ValueError('Informe um peso total válido em gramas.')
        conversion['method'] = 'provided_mass'
    elif unit == 'ml' and food['id'] in DENSITIES:
        density = DENSITIES[food['id']]
        mass = amount * density['value']
        conversion.update(density_g_per_ml=float(density['value']), density_source=density['source'],
                          density_url=density['url'], method='estimated_density')
    else:
        mass = None
        conversion.update(requires_weight=True, method='missing_mass')
    source = dataset()['source']
    facts, unavailable = [], []
    for nutrient in food['nutrients']:
        if 'value' not in nutrient:
            unavailable.append(nutrient)
            continue
        if mass is None:
            continue
        value = float((Decimal(str(nutrient['value'])) * mass / 100).quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
        facts.append({**nutrient, 'value': value, 'reference': {
            'food_id': food['id'], 'food_name': food['name'], 'source_name': source['name'],
            'source_version': source['version'], 'source_url': source['url'], 'basis_g': 100,
            'value_per_100g': nutrient['value'], 'suggested_value': value,
            'nutrient_key': nutrient['key'], 'nutrient_unit': nutrient['unit'],
            'package_amount': float(amount), 'package_unit': unit, 'package_mass_g': float(mass),
            'density_g_per_ml': conversion['density_g_per_ml'], 'density_source_url': conversion['density_url'],
        }})
    if mass is not None:
        conversion['total_grams'] = float(mass)
    return {'id': food['id'], 'name': food['name'], 'category': food['category'],
            'source_name': source['name'], 'source_version': source['version'], 'source_url': source['url'],
            'nutrition_facts': facts, 'unavailable_nutrients': unavailable, 'conversion': conversion}


def suggestions(query, amount, unit, total_grams=None):
    terms = tokens(query)
    if not terms:
        return []
    ranked = []
    for food in dataset()['foods']:
        if not is_retail_reference(food):
            continue
        words = tokens(food['name'])
        matched = sum(any(word.startswith(term) for word in words) for term in terms)
        coverage = matched / len(terms)
        if coverage < .65:
            continue
        score = coverage * 100 + (20 if terms == words else 0) - abs(len(words) - len(terms))
        # The leading name identifies the food; later words may only be ingredients.
        primary = tokens(food['name'].split(',')[0])
        primary_match = bool(terms & primary)
        if primary_match:
            score += 100
        if not terms & {'cru', 'cozido', 'cozida', 'frito', 'frita'} and 'cru' in words:
            score += 10
        if terms == {'ovo'} and primary == {'ovo'}:
            score += 10 * ('inteiro' in words) + 10 * ('galinha' in words)
        ranked.append((score, food, primary_match))
    if any(primary_match for _, _, primary_match in ranked):
        ranked = [match for match in ranked if match[2]]
    ranked.sort(key=lambda match: (-match[0], match[1]['name']))
    return [preview(food, amount, unit, total_grams) for _, food, _ in ranked[:8]]
