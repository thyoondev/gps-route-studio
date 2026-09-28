"""Line-delimited JSON bridge for the native Mac route controller."""
from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import AsyncExitStack, suppress

import gpsroute as route


class ClosingDialPlane(route.userspace_tunnel.UserspaceDialPlane):
    """Close relay clients before awaiting server shutdown on Python 3.13.

    The installed pmd dial plane only closes listening sockets, while its relay
    gathers both directions and may retain clients after the device stops sending.
    This process-local adapter avoids changing the shared pymobiledevice3 install.
    """
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        for server in self._servers:
            server.close()
            server.close_clients()
        return await super().__aexit__(exc_type, exc_val, exc_tb)


def emit(kind: str, **data: object) -> None:
    try:
        print(json.dumps(dict(kind=kind, **data), allow_nan=False), flush=True)
    except (BrokenPipeError, OSError):
        # A closed GUI must never prevent device restoration.
        pass


def number(value: object, minimum: float, maximum: float) -> float:
    result = float(value)
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"값은 {minimum}~{maximum} 범위여야 합니다.")
    return result


def settings(data: dict) -> tuple[float, float]:
    return number(data['speed'], 0.1, 300), number(data['jitter'], 0, 100)


def check_conflict() -> None:
    result = subprocess.run(['pgrep', '-f', 'GeoShift.app/Contents/(MacOS/GeoShift|Resources/keeper.py)'],
                            capture_output=True)
    if result.returncode == 0:
        raise RuntimeError('GeoShift 또는 keeper가 실행 중입니다. Restore GPS 후 종료하세요.')
    if result.returncode != 1:
        raise RuntimeError('GeoShift 실행 여부를 확인하지 못했습니다.')


async def stream(location, points: list[route.Point], state: dict, stop: asyncio.Event) -> None:
    """Advance by elapsed time; never send a burst of overdue coordinates."""
    leg = 0
    offset = 0.0
    lap = 1
    previous = None
    previous_time = None
    last_tick = time.monotonic()
    started = last_tick
    while not stop.is_set():
        now = time.monotonic()
        delta = now - last_tick
        last_tick = now
        if state.get('paused'):
            previous = previous_time = None
            await route.sleep_or_stop(stop, 0.2)
            continue
        offset += delta * state['speed'] / 3.6
        complete = False
        while offset >= route.distance_m(points[leg], points[leg + 1]):
            offset -= route.distance_m(points[leg], points[leg + 1])
            leg += 1
            if leg == len(points) - 1:
                if not state['repeat']:
                    leg -= 1
                    offset = route.distance_m(points[leg], points[leg + 1])
                    complete = True
                    break
                lap += 1
                leg = 0
        length = route.distance_m(points[leg], points[leg + 1])
        base = route.interpolate(points[leg], points[leg + 1], min(1, offset / length))
        target = route.jittered(base, state['jitter'])
        target = route.Point(max(-90, min(90, target.lat)), (target.lon + 180) % 360 - 180)
        if location is not None:
            await asyncio.wait_for(location.set(target.lat, target.lon), route.SET_TIMEOUT_SECONDS)
        sent = time.monotonic()
        speed = None if previous is None else route.distance_m(previous, target) / (sent - previous_time) * 3.6
        emit('sample', lat=target.lat, lon=target.lon, baseLat=base.lat, baseLon=base.lon,
             speed=speed, jitter=route.distance_m(base, target), elapsed=sent-started,
             lap=lap, preview=location is None)
        previous, previous_time = target, sent
        if complete:
            return
        await route.sleep_or_stop(stop, max(0, 0.5 - (time.monotonic() - now)))


async def commands(reader: asyncio.StreamReader, state: dict, stop: asyncio.Event) -> None:
    while not stop.is_set():
        line = await reader.readline()
        if not line:
            stop.set()  # Closing/crashing the GUI closes stdin and restores GPS.
            return
        try:
            message = json.loads(line)
            if message['command'] == 'stop':
                stop.set()
            elif message['command'] == 'pause':
                state['paused'] = bool(message['paused'])
                emit('status', message='일시정지 · 마지막 위치 유지' if state['paused'] else '재생 중')
            elif message['command'] == 'settings':
                state['speed'], state['jitter'] = settings(message)
        except (ValueError, KeyError, TypeError) as error:
            emit('error', message=str(error))


async def main() -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=2_000_000)
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    control = None
    tunnel = None
    contexts = AsyncExitStack()
    try:
        config = json.loads(await reader.readline())
        speed, jitter = settings(config)
        points = [route.parse_point(p) for p in config['points']]
        points = [p for i, p in enumerate(points) if i == 0 or route.distance_m(points[i-1], p) > .001]
        if not 2 <= len(points) <= 10000:
            raise ValueError('서로 다른 경유지를 2~10,000개 지정하세요.')
        if config.get('pingpong'):
            points += list(reversed(points))[1:]
        elif config.get('repeat') and points[-1] != points[0]:
            points.append(points[0])
        if any(route.central_angle(a, b) > math.pi - 1e-6 for a, b in zip(points, points[1:])):
            raise ValueError('지구 반대편을 바로 연결할 수 없습니다. 중간 경유지를 추가하세요.')
        state = dict(speed=speed, jitter=jitter, repeat=bool(config.get('repeat')))
        control = asyncio.create_task(commands(reader, state, stop))
        if config.get('preview'):
            emit('status', message='미리보기 · 기기 전송 없음')
            await stream(None, points, state, stop)
            emit('finished', message='미리보기 종료')
            return
        check_conflict()
        emit('status', message='iPhone 연결 중…')
        udid, remote = await asyncio.wait_for(route.resolve_target_device(config.get('udid') or None), 15)
        if stop.is_set():
            return
        route.userspace_tunnel.UserspaceDialPlane = ClosingDialPlane
        tunnel, rsd = await route.open_target_tunnel(udid, remote)
        if rsd.udid != udid:
            raise RuntimeError('선택한 iPhone과 연결된 기기가 다릅니다.')
        if stop.is_set():
            return
        async with asyncio.timeout(20):
            dvt = await contexts.enter_async_context(route.DvtProvider(rsd))
            location = await contexts.enter_async_context(route.LocationSimulation(dvt))
        try:
            if not stop.is_set():
                emit('status', message='iPhone에 전송 중 · 폰 수신값은 GPS Receiver에서 확인')
                await stream(location, points, state, stop)
        finally:
            emit('status', message='실제 GPS 복구 명령 전송 중…')
            try:
                await asyncio.wait_for(location.clear(), 5)
                emit('restored', message='GPS 해제 명령 전송 완료 · 폰에서 실제 위치 확인')
            except Exception as error:
                emit('restore_failed', message=f'GPS 복구 실패: {error}. GeoShift의 Restore GPS를 누르세요.')
                raise
    finally:
        if control:
            control.cancel()
            with suppress(asyncio.CancelledError):
                await control
        try:
            await asyncio.wait_for(contexts.aclose(), 5)
        except Exception as error:
            emit('error', message=f'개발자 채널 종료 오류: {error}')
        if tunnel:
            try:
                await asyncio.wait_for(tunnel.aclose(), 15)
            except Exception as error:
                emit('error', message=f'터널 종료 오류 ({type(error).__name__}): {error}. GPS 복구 결과는 위 상태를 확인하세요.')
        transport.close()


if __name__ == '__main__':
    try:
        # Per-user process lock; OS releases it even on abnormal termination.
        lock_path = os.path.join(tempfile.gettempdir(), f'gpsroute-studio-{os.getuid()}.lock')
        with open(lock_path, 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            asyncio.run(main())
    except Exception as error:
        emit('error', message=f'{type(error).__name__}: {error}')
        sys.exit(1)
