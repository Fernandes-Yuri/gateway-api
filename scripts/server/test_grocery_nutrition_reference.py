from decimal import Decimal
import pytest
from services.grocery_nutrition_reference import dataset, preview, suggestions


def food(food_id):
    return next(item for item in dataset()['foods'] if item['id'] == food_id)


def test_official_dataset_has_attribution_and_missing_values_are_not_zero():
    data = dataset()
    assert len(data['foods']) == 597
    assert data['source']['name'] == 'TACO / NEPA-UNICAMP'
    assert len(data['source']['sha256']) == 64
    oil = suggestions('Óleo de Soja', 900, 'ml')[0]
    assert oil['id'] == 'TACO:272'
    assert 'protein' not in {f['key'] for f in oil['nutrition_facts']}
    assert next(n for n in oil['unavailable_nutrients'] if n['key'] == 'protein')['status'] == 'not_applicable'


def test_soybean_oil_volume_uses_explicit_estimated_density_and_preserves_source():
    result = suggestions('oleo de soja', 900, 'ml')[0]
    assert result['conversion']['total_grams'] == 828
    assert result['conversion']['density_g_per_ml'] == .92
    assert result['conversion']['method'] == 'estimated_density'
    facts = {item['key']: item for item in result['nutrition_facts']}
    assert facts['fat']['value'] == 828
    assert facts['calories']['value'] == 7319.52
    assert facts['fat']['reference']['source_url'].startswith('https://nepa.unicamp.br/')
    assert facts['fat']['reference']['package_mass_g'] == 828
    assert facts['saturated_fat']['value'] == 125.86


def test_scaling_by_weight_keeps_raw_and_cooked_references_distinct():
    results = suggestions('arroz integral', 500, 'g')
    names = [r['name'] for r in results]
    assert 'Arroz, integral, cru' in names
    assert 'Arroz, integral, cozido' not in names
    reference = food('TACO:2')
    at_100 = {n['key']: n['value'] for n in preview(reference, 100, 'g')['nutrition_facts']}
    at_500 = {n['key']: n['value'] for n in preview(reference, 500, 'g')['nutrition_facts']}
    assert abs(at_500['calories'] - 5 * at_100['calories']) < .03


def test_no_general_volume_to_mass_or_piece_weight_assumption():
    milk = suggestions('leite', 1000, 'ml')[0]
    assert milk['conversion']['requires_weight']
    assert milk['nutrition_facts'] == []
    eggs = suggestions('ovo', 12, 'un')[0]
    assert eggs['conversion']['requires_weight']
    with_mass = preview(food(eggs['id']), 12, 'un', 600)
    assert with_mass['conversion']['total_grams'] == 600
    assert with_mass['conversion']['method'] == 'provided_mass'
    assert with_mass['nutrition_facts']


@pytest.mark.parametrize('amount,unit', [(0,'g'),(-1,'ml'),(float('nan'),'g'),(float('inf'),'g'),(1,'L'),(1.5,'un')])
def test_invalid_package_is_rejected(amount, unit):
    with pytest.raises(ValueError):
        preview(food('TACO:272'), amount, unit)


def test_unknown_food_does_not_invent_a_reference():
    assert suggestions('xyzfoodinexistente', Decimal('900'), 'ml') == []


@pytest.mark.parametrize('query', ['ovo', 'ovos', 'Ovos'])
def test_eggs_prioritize_whole_raw_chicken_and_exclude_products_containing_eggs(query):
    results = suggestions(query, 20, 'un', 760)
    assert results[0]['id'] == 'TACO:489'
    assert all(result['name'].startswith('Ovo,') for result in results)
    assert results[0]['conversion']['total_grams'] == 760
    assert results[0]['conversion']['package_amount'] == 20
    expected = preview(food('TACO:489'), 760, 'g')['nutrition_facts']
    assert [n['value'] for n in results[0]['nutrition_facts']] == [n['value'] for n in expected]


def test_specific_egg_and_prepared_product_queries_remain_available():
    assert suggestions('ovo codorna', 20, 'un')[0]['id'] == 'TACO:485'
    assert suggestions('ovo frito', 100, 'g') == []
    assert suggestions('maionese', 500, 'g')[0]['id'] == 'TACO:524'
    assert suggestions('macarrao ovos', 500, 'g')[0]['id'] == 'TACO:41'


@pytest.mark.parametrize('query', ['tapioca', 'goma de tapioca', 'tapioca com manteiga',
                                  'feijao tropeiro', 'arroz carreteiro'])
def test_missing_plain_food_never_falls_back_to_a_composed_dish(query):
    assert suggestions(query, 500, 'g') == []


@pytest.mark.parametrize('query', ['arroz', 'feijao', 'frango', 'ovo', 'batata'])
def test_staples_only_offer_uncooked_references(query):
    results = suggestions(query, 500, 'g')
    assert results
    assert all(not any(state in result['name'] for state in
                       ['cozido', 'cozida', 'grelhado', 'grelhada', 'refogado', 'refogada'])
               for result in results)


@pytest.mark.parametrize('query', ['leite', 'oleo de soja', 'farinha', 'polvilho doce', 'manteiga'])
def test_market_products_do_not_require_a_literal_raw_label(query):
    assert suggestions(query, 500, 'g')
