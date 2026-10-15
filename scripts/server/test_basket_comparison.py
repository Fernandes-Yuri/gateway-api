from decimal import Decimal
import pytest
from pydantic import ValidationError
from schemas.grocery_cards import BasketComparisonRequest
from services.grocery_cards import compare_basket


def basket(items):
    return BasketComparisonRequest(items=items).items


def market(id):
    return dict(id=id, name=id, logo_url='https://example.org/logo.png', website_url='https://example.org')


def card(id, kcal, **quotes):
    return dict(id=id, kcal_per_package=kcal, markets=[dict(market_id=m, price=Decimal(p)) for m, p in quotes.items()])


def test_incomplete_cheap_market_cannot_win():
    result = compare_basket(basket([dict(card_id='butter', quantity=2), dict(card_id='eggs', quantity=1)]),
        [card('butter', 1400, a='12.90', b='11.00', partial='1.00'), card('eggs', 840, a='12.00', b='18.00')],
        [market('a'), market('b'), market('partial')])
    assert result.total_items == 3
    assert result.total_planned_kcal == 3640
    assert result.best_market.market_id == 'a'
    assert result.best_market.total_brl == Decimal('37.80')
    assert result.best_market.savings_vs_highest_brl == Decimal('2.20')
    assert result.market_rankings[-1].missing_items_count == 1
    assert not result.market_rankings[-1].is_best_deal


def test_duplicates_aggregate_and_ties_are_explicit():
    result = compare_basket(basket([dict(card_id='a', quantity=1), dict(card_id='a', quantity=2)]),
        [card('a', 320, x='0.10', y='0.10')], [market('y'), market('x')])
    assert result.best_market.total_brl == Decimal('0.30')
    assert result.total_planned_kcal == 960
    assert all(r.is_best_deal for r in result.market_rankings)


def test_no_complete_basket_and_empty_basket():
    result = compare_basket(basket([dict(card_id='a', quantity=1)]), [card('a', 0)], [market('x')])
    assert result.best_market is None
    assert not result.market_rankings[0].is_best_deal
    assert compare_basket([], [], [market('x')]).market_rankings == []


def test_unknown_or_inactive_cards_do_not_silently_disappear():
    with pytest.raises(ValueError):
        compare_basket(basket([dict(card_id='missing', quantity=1)]), [], [market('x')])


@pytest.mark.parametrize('quantity', [0, -1, 1.5, True, '2', 1000])
def test_invalid_package_quantities(quantity):
    with pytest.raises(ValidationError):
        basket([dict(card_id='a', quantity=quantity)])


def test_duplicate_quantity_limit():
    with pytest.raises(ValueError):
        compare_basket(basket([dict(card_id='a', quantity=999), dict(card_id='a', quantity=1)]), [card('a', 1)], [])
