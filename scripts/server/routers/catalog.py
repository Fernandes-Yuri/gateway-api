"""Compatibilidade temporária: alimentação encerrada, sem acesso ao banco."""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()

async def retired_nutrition():
    return JSONResponse(
        status_code=410,
        content={"detail": "O módulo de alimentação foi descontinuado.", "code": "nutrition_retired"},
        headers={"Cache-Control": "no-store"},
    )


# Caminhos antigos são mantidos somente para não encaminhar gravações ao banco.
router.add_api_route('/api/admin/grocery-prices/import-csv', retired_nutrition, methods=['POST'], include_in_schema=False)
router.add_api_route('/api/catalog/grocery-prices', retired_nutrition, methods=['GET'], include_in_schema=False)
router.add_api_route('/api/admin/grocery-prices', retired_nutrition, methods=['GET'], include_in_schema=False)
router.add_api_route('/api/admin/grocery-prices/missing', retired_nutrition, methods=['GET'], include_in_schema=False)
router.add_api_route('/api/admin/grocery-prices/{food_name}', retired_nutrition, methods=['PUT'], include_in_schema=False)
router.add_api_route('/api/admin/grocery-prices/ingredient/{slug}', retired_nutrition, methods=['PUT'], include_in_schema=False)
router.add_api_route('/api/admin/grocery-prices/import-ingredients', retired_nutrition, methods=['POST'], include_in_schema=False)
router.add_api_route('/api/admin/grocery-ingredients/import', retired_nutrition, methods=['POST'], include_in_schema=False)
router.add_api_route('/api/admin/grocery-ingredients', retired_nutrition, methods=['POST'], include_in_schema=False)
router.add_api_route('/api/admin/grocery-ingredients/{slug}', retired_nutrition, methods=['PATCH'], include_in_schema=False)
router.add_api_route('/api/admin/grocery-ingredients/{slug}', retired_nutrition, methods=['DELETE'], include_in_schema=False)
router.add_api_route('/api/admin/grocery-prices/refresh', retired_nutrition, methods=['POST'], include_in_schema=False)
router.add_api_route('/api/catalog/grocery-price-contributions', retired_nutrition, methods=['POST'], include_in_schema=False)
router.add_api_route('/api/catalog/grocery-price-contributions/averages', retired_nutrition, methods=['GET'], include_in_schema=False)
router.add_api_route('/api/catalog/grocery-ingredients', retired_nutrition, methods=['GET'], include_in_schema=False)
router.add_api_route('/api/catalog/nutrition', retired_nutrition, methods=['GET'], include_in_schema=False)
router.add_api_route('/api/catalog/grocery/cards', retired_nutrition, methods=['GET'], include_in_schema=False)
router.add_api_route('/api/admin/grocery/retail', retired_nutrition, methods=['GET'], include_in_schema=False)
router.add_api_route('/api/admin/grocery/retail', retired_nutrition, methods=['PUT'], include_in_schema=False)
router.add_api_route('/api/admin/grocery/nutrition-suggestions', retired_nutrition, methods=['GET'], include_in_schema=False)
router.add_api_route('/api/catalog/grocery/basket-comparison', retired_nutrition, methods=['POST'], include_in_schema=False)
