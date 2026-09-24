import unittest

from desktop.capture_context import capture_readiness, normalize_share_context, valid_text


class CaptureContextTests(unittest.TestCase):
    def test_snapshot_preserves_scalar_types_and_distinct_values(self):
        value = {'capturedAt': 1, 'headers': {'user-agent': 'iOS SDK', 'gl_dev_id': 'header-id'},
                 'security': {'geelyDeviceId': 'nested-id', 'osVersion': '18', 'battery': 70,
                              'isRoot': False, 'lbsLatitude': '', 'channel': '%E5%90%89'}}
        result = normalize_share_context(value)
        self.assertEqual(result, value)
        value['security']['geelyDeviceId'] = 'changed'
        self.assertEqual(result['security']['geelyDeviceId'], 'nested-id')

    def test_snapshot_keeps_captured_share_security_names_and_string_values(self):
        security = {'deviceUUID': 'uuid-value', 'isJailbreak': 'false',
                    'isLBSEnabled': 'true', 'isUsingVpn': 'false',
                    'os_version': '18.0', 'ua': 'captured-ua', 'wifiMAC': 'masked-mac'}
        result = normalize_share_context({'capturedAt': 1, 'headers': {'user-agent': 'sdk'},
                                          'security': security})
        self.assertEqual(result['security'], security)

    def test_invalid_fields_cannot_enter_snapshot(self):
        for field in ({'token': 'secret'}, {'battery': []}, {'battery': float('nan')}, {'wifiName': 'bad\nheader'}):
            self.assertIsNone(normalize_share_context({'capturedAt': 1, 'headers': {'user-agent': 'sdk'}, 'security': field}))
        self.assertIsNone(normalize_share_context({'capturedAt': True, 'headers': {'user-agent': 'sdk'}, 'security': {'battery': '1'}}))
        self.assertFalse(valid_text('   '))
        self.assertFalse(valid_text('sdk\x7f'))

    def test_incomplete_input_exposes_names_and_booleans_only(self):
        status = capture_readiness({'token': 'secret', 'deviceName': 'private-device'})
        self.assertFalse(status['readiness']['login'])
        self.assertFalse(status['readiness']['device'])
        self.assertFalse(status['readiness']['share'])
        self.assertNotIn('private-device', str(status))
        self.assertNotIn('secret', str(status))
