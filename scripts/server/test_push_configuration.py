"""Configuração FCM com SDK falso; nenhum envio externo."""
import unittest
from unittest.mock import MagicMock
from core.devices import dispatch_push


class PushConfigurationTest(unittest.TestCase):
    def test_data_only_alert_is_sent_without_a_system_notification(self):
        sdk = MagicMock()
        data = {'type': 'HEALTH_ALERT', 'sourceUserId': 'patient', 'title': 'Aviso', 'body': 'Teste'}
        self.assertTrue(dispatch_push('test-token', 'Aviso', 'Teste', data,
            include_notification=False, messaging=sdk, has_fcm=True))
        self.assertIsNone(sdk.Message.call_args.kwargs['notification'])
        self.assertIsNone(sdk.AndroidConfig.call_args.kwargs['notification'])
        self.assertEqual(sdk.AndroidConfig.call_args.kwargs['priority'], 'high')
        self.assertEqual(sdk.Message.call_args.kwargs['data'], data)
        sdk.send.assert_called_once_with(sdk.Message.return_value)

    def test_sdk_failure_is_not_accepted(self):
        sdk = MagicMock()
        sdk.send.side_effect = RuntimeError('FCM rejected')
        self.assertFalse(dispatch_push('test-token', 'Aviso', 'Teste', messaging=sdk, has_fcm=True))
