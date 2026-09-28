import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import android_worker as android


class ParserTests(unittest.TestCase):
    def test_only_authorized_devices(self):
        self.assertEqual(android.parse_devices('List of devices attached\nphone device\nold unauthorized\noff offline\nemulator-5554 device\n1.2.3.4:5555 device\n'), ['phone'])

    def test_previous_permission_is_preserved(self):
        for mode in ('allow', 'deny', 'ignore', 'default', 'foreground'):
            self.assertEqual(android.mock_mode(f'MOCK_LOCATION: {mode}; time=+1s'), mode)
        self.assertEqual(android.mock_mode('No operations.\nDefault mode: deny'), 'default')
        with self.assertRaises(RuntimeError): android.mock_mode('unrecognized response')


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_restores_device_before_revoking_permission(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(android, 'RECOVERY_ROOT', Path(folder)):
            path = android.save_recovery('phone', 'deny')
            calls = []
            async def adb(*args, **kwargs): calls.append(args)
            async def restore(device): calls.append(('verified', device))
            with patch.object(android, 'adb', adb), patch.object(android, 'confirm_restoration', restore), patch.object(android, 'emit'):
                await android.recover_previous('phone')
            self.assertEqual(calls[0][-1], 'allow')
            self.assertEqual(calls[1], ('verified', 'phone'))
            self.assertEqual(calls[2][-1], 'deny')
            self.assertFalse(path.exists())

    async def test_failed_device_restore_keeps_journal_and_permission(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(android, 'RECOVERY_ROOT', Path(folder)):
            path = android.save_recovery('phone', 'default')
            adb = AsyncMock(return_value='')
            with patch.object(android, 'adb', adb), patch.object(android, 'confirm_restoration', AsyncMock(side_effect=RuntimeError('offline'))), patch.object(android, 'emit'):
                with self.assertRaises(RuntimeError): await android.recover_previous('phone')
            self.assertTrue(path.exists())
            self.assertEqual(adb.await_count, 1)
            self.assertEqual(adb.await_args.args[-1], 'allow')

    async def test_failed_permission_restore_retains_journal(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(android, 'RECOVERY_ROOT', Path(folder)):
            path = android.save_recovery('phone', 'ignore')
            with patch.object(android, 'adb', AsyncMock(side_effect=['', RuntimeError('offline')])), patch.object(android, 'confirm_restoration', AsyncMock()), patch.object(android, 'emit'):
                with self.assertRaises(RuntimeError): await android.recover_previous('phone')
            self.assertEqual(json.loads(path.read_text())['mode'], 'ignore')

    async def test_device_identity_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(android, 'RECOVERY_ROOT', Path(folder)):
            path = android.save_recovery('phone', 'default')
            path.write_text(json.dumps(dict(device='other', mode='default')))
            adb = AsyncMock()
            with patch.object(android, 'adb', adb):
                with self.assertRaises(RuntimeError): await android.recover_previous('phone')
            adb.assert_not_awaited()

    async def test_matching_nonce_with_failure_is_not_success(self):
        with patch.object(android.secrets, 'token_hex', return_value='nonce'), patch.object(android, 'adb', AsyncMock(side_effect=['Starting...', json.dumps(dict(token='nonce', restored=False, error='denied'))])):
            with self.assertRaisesRegex(RuntimeError, 'denied'):
                await android.confirm_restoration('phone')


if __name__ == '__main__': unittest.main()
