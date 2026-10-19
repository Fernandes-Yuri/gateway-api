import unittest
from unittest.mock import AsyncMock, patch

from services.ingredient_admin import update_ingredient, validate_payload


class LegacyIngredientNameTests(unittest.IsolatedAsyncioTestCase):
    async def _preserves_name(self, name):
        conn = AsyncMock()
        conn.fetchrow.return_value = {
            'slug': 'existing', 'name': name, 'unit': 'kg', 'details': {}, 'active': True,
        }
        with patch('services.ingredient_admin._taken_names', AsyncMock(return_value=set())):
            result = await update_ingredient(conn, 'existing', {'name': name, 'group': 'Alimentos'}, bump=False)
        self.assertEqual(result['name'], name)
        self.assertEqual(conn.execute.call_args_list[0].args[2], name)

    async def test_coffee_name_is_preserved(self):
        await self._preserves_name('Café Torrado e Moído Tradicional')

    async def test_cheese_name_is_preserved(self):
        await self._preserves_name('Queijo Mussarela Fatiado')

    async def test_new_preparation_name_still_rejected_without_writes(self):
        conn = AsyncMock()
        conn.fetchrow.return_value = {
            'slug': 'existing', 'name': 'Café', 'unit': 'kg', 'details': {}, 'active': True,
        }
        with self.assertRaisesRegex(ValueError, 'termo de preparo ou corte'):
            await update_ingredient(conn, 'existing', {'name': 'Café Moído'}, bump=False)
        conn.execute.assert_not_awaited()

    def test_create_and_unknown_fields_still_rejected(self):
        with self.assertRaisesRegex(ValueError, 'termo de preparo ou corte'):
            validate_payload({'name': 'Café Moído', 'unit': 'kg'})
        with self.assertRaisesRegex(ValueError, 'Campos desconhecidos'):
            validate_payload({'name': 'Café Moído', 'unexpected': True}, partial=True, existing_name='Café Moído')
