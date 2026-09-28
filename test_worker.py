import asyncio
import io
import json
import math
import unittest
from unittest.mock import patch

import worker
from gpsroute import Point


class ValidationTests(unittest.TestCase):
    def test_finite_settings(self):
        for value in (float('nan'), float('inf'), -1, 301):
            with self.assertRaises(ValueError):
                worker.settings({'speed': value, 'jitter': 3})

    def test_negative_jitter_rejected(self):
        with self.assertRaises(ValueError):
            worker.settings({'speed': 40, 'jitter': -1})

    def test_conflict_fails_closed(self):
        from types import SimpleNamespace
        for code in (0, 2, 3):
            with patch.object(worker.subprocess, 'run', return_value=SimpleNamespace(returncode=code)):
                with self.assertRaises(RuntimeError):
                    worker.check_conflict()


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_relay_clients_closed_before_wait(self):
        from unittest.mock import AsyncMock, Mock
        from types import SimpleNamespace
        server = SimpleNamespace(close=Mock(), close_clients=Mock())
        async def wait_closed(): server.close_clients.assert_called_once()
        server.wait_closed = wait_closed
        plane = object.__new__(worker.ClosingDialPlane)
        plane._servers = [server]; plane._relays = {'test': 1}
        await plane.__aexit__(None, None, None)
        self.assertEqual(plane._servers, [])
        self.assertEqual(plane._relays, {})

    async def test_endpoint_sent(self):
        samples = []
        class Device:
            async def set(self, lat, lon): samples.append((lat, lon))
        with patch.object(worker, 'emit'):
            await worker.stream(Device(), [Point(37, 127), Point(37.000001, 127)],
                                {'speed': 300, 'jitter': 0, 'repeat': False}, asyncio.Event())
        self.assertEqual(samples[-1], (37.000001, 127.0))

    async def test_stopped_does_not_send(self):
        stop = asyncio.Event(); stop.set()
        with patch.object(worker, 'emit') as emit:
            await worker.stream(None, [Point(37, 127), Point(38, 127)],
                                {'speed': 40, 'jitter': 0, 'repeat': False}, stop)
            emit.assert_not_called()

    async def test_stdin_eof_requests_restore(self):
        reader = asyncio.StreamReader(); reader.feed_eof()
        stop = asyncio.Event()
        await worker.commands(reader, {}, stop)
        self.assertTrue(stop.is_set())

    async def test_live_settings_and_pause(self):
        reader = asyncio.StreamReader()
        for event in ({'command': 'settings', 'speed': 15, 'jitter': 2}, {'command': 'pause', 'paused': True}, {'command': 'stop'}):
            reader.feed_data((json.dumps(event)+'\n').encode())
        state = {}; stop = asyncio.Event()
        with patch.object(worker, 'emit'):
            await worker.commands(reader, state, stop)
        self.assertEqual(state, {'speed': 15, 'jitter': 2, 'paused': True})
        self.assertTrue(stop.is_set())

    async def test_device_failure_propagates_to_restore_scope(self):
        class Device:
            async def set(self, lat, lon): raise ConnectionError('lost')
        with self.assertRaises(ConnectionError):
            await worker.stream(Device(), [Point(37, 127), Point(38, 127)],
                                {'speed': 40, 'jitter': 0, 'repeat': False}, asyncio.Event())


class RestoreTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, stream_failure=False, broken_output=False, entry_failure=False):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        reader = asyncio.StreamReader()
        reader.feed_data((json.dumps({'points': ['37,127', '37.1,127'], 'speed': 40, 'jitter': 0}) + '\n').encode())
        class Context:
            def __init__(self, value): self.value = value
            async def __aenter__(self):
                if entry_failure: raise TimeoutError('handshake')
                return self.value
            async def __aexit__(self, *args): pass
        location = SimpleNamespace(clear=AsyncMock())
        tunnel = SimpleNamespace(aclose=AsyncMock())
        transport = SimpleNamespace(close=lambda: None)
        loop = asyncio.get_running_loop()
        async def pipe(factory, stdin): return transport, None
        async def playback(*args):
            if stream_failure: raise ConnectionError('send failed')
        with patch.object(worker.asyncio, 'StreamReader', return_value=reader), \
             patch.object(loop, 'connect_read_pipe', side_effect=pipe), \
             patch.object(loop, 'add_signal_handler'), \
             patch.object(worker, 'check_conflict'), \
             patch.object(worker.route, 'resolve_target_device', AsyncMock(return_value=('test', False))), \
             patch.object(worker.route, 'open_target_tunnel', AsyncMock(return_value=(tunnel, SimpleNamespace(udid='test')))), \
             patch.object(worker.route, 'DvtProvider', return_value=Context(None)), \
             patch.object(worker.route, 'LocationSimulation', return_value=Context(location)), \
             patch.object(worker, 'stream', side_effect=playback), \
             patch('builtins.print', side_effect=BrokenPipeError if broken_output else None):
            if stream_failure or entry_failure:
                with self.assertRaises((ConnectionError, TimeoutError)): await worker.main()
            else: await worker.main()
        if not entry_failure: location.clear.assert_awaited_once()
        tunnel.aclose.assert_awaited_once()

    async def test_restore_after_send_failure(self): await self.exercise(stream_failure=True)
    async def test_restore_when_gui_output_is_gone(self): await self.exercise(broken_output=True)
    async def test_tunnel_closed_after_handshake_failure(self): await self.exercise(entry_failure=True)
    async def test_normal_completion_restores(self): await self.exercise()


if __name__ == '__main__': unittest.main()
