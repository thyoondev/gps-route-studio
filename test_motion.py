import asyncio
import json
import plistlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import motion_worker as motion


def config(**values):
    return dict(points=['37,127', '37.01,127', '37.01,127.01'], speed=36, jitter=0, **values)


class PlaybackTests(unittest.TestCase):
    def test_speed_change_changes_progress_without_jump(self):
        player = motion.Playback(config())
        state = dict(speed=36, jitter=0)
        player.sample(1, state)
        self.assertAlmostEqual(player.offset, 10)
        state['speed'] = 72
        fix = player.sample(1, state)
        self.assertAlmostEqual(player.offset, 30)
        self.assertEqual(fix['speed'], 72)

    def test_pause_holds_noisy_fix_and_transmits_zero_speed(self):
        player = motion.Playback(config())
        state = dict(speed=36, jitter=3)
        first = player.sample(1, state)
        state['paused'] = True
        for _ in range(5):
            paused = player.sample(5, state)
            self.assertEqual((paused['lat'], paused['lon']), (first['lat'], first['lon']))
            self.assertEqual(paused['speed'], 0)
        self.assertAlmostEqual(player.offset, 10)
        state['paused'] = False
        player.sample(1, state)
        self.assertAlmostEqual(player.offset, 20)

    def test_endpoint_stops_and_pingpong_returns(self):
        player = motion.Playback(config(pingpong=True))
        fix = player.sample(100000, dict(speed=36, jitter=0))
        self.assertTrue(player.complete)
        self.assertAlmostEqual(fix['lat'], 37)
        self.assertAlmostEqual(fix['lon'], 127)
        self.assertEqual(fix['speed'], 0)

    def test_repeat_large_delay_and_nonzero_leg(self):
        player = motion.Playback(config(repeat=True))
        state = dict(speed=36, jitter=0)
        player.sample(player.lengths[0] / 10 + 1, state)
        self.assertEqual(player.leg, 1)
        before = player.sample(0, state)
        after = player.sample(sum(player.lengths) * 100 / 10, state)
        self.assertEqual(after['lap'], 101)
        self.assertAlmostEqual(before['lat'], after['lat'])
        self.assertAlmostEqual(before['lon'], after['lon'])

    def test_invalid_routes_rejected(self):
        for points in (['37,127'], ['37,127', '37,127'], ['91,127', '37,127']):
            with self.assertRaises((ValueError, SystemExit)):
                motion.Playback({**config(), 'points': points})

    def test_jitter_setting_applies_next_sample(self):
        player = motion.Playback(config())
        with patch.object(motion.route, 'jittered', wraps=motion.route.jittered) as jitter:
            player.sample(1, dict(speed=36, jitter=12))
            self.assertEqual(jitter.call_args.args[1], 12)

    def test_runfile_is_private_and_passes_session_to_runner(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'base.xctestrun'
            output = Path(folder) / 'live.xctestrun'
            source.write_bytes(plistlib.dumps({'GPSMotionUITests': {'EnvironmentVariables': {'KEEP': 'yes'}}}))
            motion.configure_run(source, output, 45555, 'test-token')
            data = plistlib.loads(output.read_bytes())['GPSMotionUITests']
            self.assertEqual(data['EnvironmentVariables']['KEEP'], 'yes')
            self.assertEqual(data['EnvironmentVariables']['GPS_STUDIO_PORT'], '45555')
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, response, command='fix'):
        reader = asyncio.StreamReader()
        if response is not None:
            reader.feed_data(json.dumps(response).encode() + b'\n')
        else:
            reader.feed_eof()
        from unittest.mock import AsyncMock, Mock
        writer = Mock(drain=AsyncMock())
        connection = motion.MotionConnection(reader, writer, 'test-token')
        return await connection.request(command, speed=40)

    async def test_applied_ack_is_required(self):
        await self.exercise({'sequence': 1, 'applied': True})
        for response in ({'sequence': 2, 'applied': True}, {'sequence': 1}, None):
            with self.assertRaises(ConnectionError):
                await self.exercise(response)

    async def test_restoration_ack_is_required(self):
        await self.exercise({'sequence': 1, 'restored': True}, 'stop')
        with self.assertRaises(ConnectionError):
            await self.exercise({'sequence': 1, 'applied': True}, 'stop')


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, restore_failure=False, send_failure=False):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        reader = asyncio.StreamReader()
        reader.feed_data(json.dumps(config()).encode() + b'\n')
        calls = []
        async def request(command, **values):
            calls.append(command)
            if command == 'fix':
                reader.feed_data(b'{"command":"stop"}\n')
                if send_failure:
                    raise ConnectionError('send')
            if command == 'stop' and restore_failure:
                raise ConnectionError('restore')
        connection = SimpleNamespace(request=request, close=AsyncMock())
        process = SimpleNamespace(returncode=0)
        transport = SimpleNamespace(close=lambda: None)
        loop = asyncio.get_running_loop()
        with tempfile.TemporaryDirectory() as folder:
            products = Path(folder) / f'gpsroute-studio-motion-{motion.os.getuid()}' / 'Build' / 'Products'
            products.mkdir(parents=True)
            (products / 'GPSMotion_test.xctestrun').write_bytes(plistlib.dumps({'GPSMotionUITests': {'EnvironmentVariables': {}}}))
            with patch.object(motion.tempfile, 'gettempdir', return_value=folder), \
                 patch.object(motion.asyncio, 'StreamReader', return_value=reader), \
                 patch.object(loop, 'connect_read_pipe', AsyncMock(return_value=(transport, None))), \
                 patch.object(loop, 'add_signal_handler'), \
                 patch.object(motion, 'check_conflict'), \
                 patch.object(motion.usbmux, 'list_devices', AsyncMock(return_value=[SimpleNamespace(is_usb=True, serial='test')])), \
                 patch.object(motion, 'build', AsyncMock(return_value=True)), \
                 patch.object(motion.asyncio, 'create_subprocess_exec', AsyncMock(return_value=process)), \
                 patch.object(motion, 'connect', AsyncMock(return_value=connection)), \
                 patch.object(motion, 'end_process', AsyncMock()), \
                 patch.object(motion, 'emit'):
                if restore_failure or send_failure:
                    with self.assertRaises((RuntimeError, ConnectionError)):
                        await motion.main()
                else:
                    await motion.main()
        self.assertEqual(calls, ['fix', 'stop'])
        connection.close.assert_awaited_once()

    async def test_clean_stop_restores(self):
        await self.exercise()

    async def test_missing_restore_ack_is_failure(self):
        await self.exercise(restore_failure=True)

    async def test_send_failure_still_clears(self):
        await self.exercise(send_failure=True)


if __name__ == '__main__':
    unittest.main()
