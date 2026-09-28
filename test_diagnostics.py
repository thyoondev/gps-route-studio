import asyncio
import json
import sys
import unittest
from unittest.mock import patch

from diagnostic_worker import PREFIX, decode_snapshot, end_process, supervised


class TelemetryTests(unittest.TestCase):
    def test_ignores_unrelated_console_output(self):
        self.assertIsNone(decode_snapshot(b'Location simulation succeeded'))
        self.assertIsNone(decode_snapshot(b'Other: GPS_DIAGNOSTIC {}'))

    def test_retains_unknown_source_and_inactive_state(self):
        snapshot = dict(version=1, active=False, readings={'source': '확인 불가'}, observation='측정 중지')
        self.assertEqual(decode_snapshot(PREFIX + json.dumps(snapshot).encode()), snapshot)

    def test_rejects_malformed_telemetry(self):
        for item in ([], {}, {'version': 2}, dict(version=1, active='false', readings={}, observation=''),
                     dict(version=1, active=True, readings={'source': False}, observation='')):
            with self.subTest(item=item), self.assertRaises(ValueError):
                decode_snapshot(PREFIX + json.dumps(item).encode())

    def test_preserves_simulation_disclosure(self):
        snapshot = dict(version=1, active=True, readings={'source': '소프트웨어 시뮬레이션'}, observation='판정 불명')
        self.assertEqual(decode_snapshot(PREFIX + json.dumps(snapshot).encode())['readings']['source'], '소프트웨어 시뮬레이션')


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_parent_exit_cancels_active_work_and_runs_cleanup(self):
        cleaned = False

        async def pending_work():
            nonlocal cleaned
            try:
                await asyncio.Event().wait()
            finally:
                cleaned = True

        with patch('diagnostic_worker.os.getppid', side_effect=[2345, 1]), \
             patch('diagnostic_worker.main', pending_work), patch('diagnostic_worker.emit'):
            await asyncio.wait_for(supervised(), 2)
        self.assertTrue(cleaned)

    async def test_stops_owned_process(self):
        child = await asyncio.create_subprocess_exec(sys.executable, '-c', 'import time; time.sleep(60)',
                                                     stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await end_process(child)
        self.assertIsNotNone(child.returncode)
        await end_process(child)


if __name__ == '__main__':
    unittest.main()
