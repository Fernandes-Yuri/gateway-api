"""Templates e destino autenticado, sem SMTP real."""
import unittest
from unittest.mock import patch
import email_service as email


class AccountEmailBrand(unittest.IsolatedAsyncioTestCase):
    def test_otp_templates_share_emerald_palette_and_escape_code(self):
        for render in (email._otp_html, email._reset_html):
            body = render('123456')
            for color in ('#00A884', '#008F70', '#F0FDF4'):
                self.assertIn(color, body)
            for legacy in ('#2563EB', '#1D4ED8', '#F59E0B', '#D97706'):
                self.assertNotIn(legacy, body)
            self.assertIn('123456', body)

    async def test_receipt_escapes_name_and_does_not_claim_audit_was_deleted(self):
        body = email._account_lifecycle_html('<img src=x>', paused=False)
        self.assertIn('&lt;img src=x&gt;', body)
        self.assertNotIn('<img src=x>', body)
        self.assertIn('auditoria', body)
        with patch.object(email, '_send_gmail_sync', return_value=True) as send:
            result = await email.send_account_deletion_email('owner@example.test', 'Pessoa')
        self.assertTrue(result['success'])
        self.assertEqual(send.call_args.args[0], 'owner@example.test')

    def test_fallback_triggers_when_primary_fails(self):
        with patch.object(email, '_is_primary_configured', return_value=True), \
             patch.object(email, '_is_fallback_configured', return_value=True), \
             patch.object(email, '_send_smtp_payload') as mock_send:
            # Primário falha, fallback tem sucesso
            mock_send.side_effect = [RuntimeError("SMTP Primary Auth Failed"), True]

            success = email._send_gmail_sync('user@example.test', 'Assunto', '<b>HTML</b>', 'Texto')
            self.assertTrue(success)
            self.assertEqual(mock_send.call_count, 2)
            # Primeira chamada: primário
            self.assertEqual(mock_send.call_args_list[0].args[4], email.GMAIL_FROM_EMAIL)
            # Segunda chamada: fallback
            self.assertEqual(mock_send.call_args_list[1].args[4], email.FALLBACK_FROM_EMAIL)

    def test_primary_success_does_not_trigger_fallback(self):
        with patch.object(email, '_is_primary_configured', return_value=True), \
             patch.object(email, '_is_fallback_configured', return_value=True), \
             patch.object(email, '_send_smtp_payload', return_value=True) as mock_send:
            success = email._send_gmail_sync('user@example.test', 'Assunto', '<b>HTML</b>', 'Texto')
            self.assertTrue(success)
            self.assertEqual(mock_send.call_count, 1)
