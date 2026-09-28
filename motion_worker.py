"""GPS Route Studio's USB controller for full CLLocation XCTest playback."""
from __future__ import annotations

import asyncio
import fcntl
import json
import math
import os
from pathlib import Path
import plistlib
import secrets
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress

from pymobiledevice3 import usbmux
from pymobiledevice3.exceptions import PyMobileDevice3Exception

import gpsroute as route
from worker import commands, emit, settings


class Playback:
    """Route time advances only while playing; pause holds the last noisy fix."""
    def __init__(self, config: dict):
        settings(config)
        points = [route.parse_point(value) for value in config['points']]
        points = [point for i, point in enumerate(points)
                  if i == 0 or route.distance_m(points[i - 1], point) > .001]
        if not 2 <= len(points) <= 10000:
            raise ValueError('서로 다른 경유지를 2~10,000개 지정하세요.')
        if config.get('pingpong'):
            points += list(reversed(points))[1:]
        elif config.get('repeat') and points[-1] != points[0]:
            points.append(points[0])
        if any(route.central_angle(a, b) > math.pi - 1e-6 for a, b in zip(points, points[1:])):
            raise ValueError('지구 반대편을 바로 연결할 수 없습니다. 중간 경유지를 추가하세요.')
        self.points = points
        self.lengths = [route.distance_m(a, b) for a, b in zip(points, points[1:])]
        self.repeat = bool(config.get('repeat'))
        self.leg = 0
        self.offset = 0.0
        self.lap = 1
        self.complete = False
        self.last = None

    def sample(self, delta: float, state: dict) -> dict:
        paused = bool(state.get('paused'))
        if paused and self.last is not None:
            return {**self.last, 'speed': 0.0}
        if not paused:
            self.offset += max(0, delta) * state['speed'] / 3.6
        # Skip whole cycles after a long scheduling delay without a large loop.
        total = sum(self.lengths)
        if self.repeat and self.offset >= total:
            cycles = int(self.offset / total)
            self.lap += cycles
            self.offset %= total
        while self.offset >= self.lengths[self.leg]:
            self.offset -= self.lengths[self.leg]
            self.leg += 1
            if self.leg == len(self.lengths):
                if self.repeat:
                    self.leg = 0
                    self.lap += 1
                else:
                    self.leg -= 1
                    self.offset = self.lengths[self.leg]
                    self.complete = True
                    break
        a, b = self.points[self.leg:self.leg + 2]
        base = route.interpolate(a, b, min(1, self.offset / self.lengths[self.leg]))
        target = route.jittered(base, state['jitter'])
        target = route.Point(max(-90, min(90, target.lat)), (target.lon + 180) % 360 - 180)
        lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
        delta_lon = math.radians(b.lon - a.lon)
        course = math.degrees(math.atan2(math.sin(delta_lon) * math.cos(lat2),
                            math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon))) % 360
        self.last = dict(lat=target.lat, lon=target.lon, course=course,
                         speed=0.0 if paused or self.complete else state['speed'],
                         jitter=route.distance_m(base, target), lap=self.lap)
        return self.last.copy()


class MotionConnection:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, token: str):
        self.reader, self.writer, self.token = reader, writer, token
        self.sequence = 0

    async def request(self, command: str, **values) -> dict:
        self.sequence += 1
        packet = dict(command=command, token=self.token, sequence=self.sequence, **values)
        async with asyncio.timeout(4):
            self.writer.write(json.dumps(packet, allow_nan=False).encode() + b'\n')
            await self.writer.drain()
            line = await self.reader.readline()
            if not line:
                raise ConnectionError('iPhone 제어 연결이 종료됐습니다.')
            reply = json.loads(line)
            if reply.get('sequence') != self.sequence:
                raise ConnectionError('iPhone 응답 순서가 맞지 않습니다.')
            key = 'restored' if command == 'stop' else 'applied'
            if reply.get(key) is not True:
                raise ConnectionError('iPhone 적용 확인을 받지 못했습니다.')
            return reply

    async def close(self):
        self.writer.close()
        with suppress(OSError, asyncio.TimeoutError):
            await asyncio.wait_for(self.writer.wait_closed(), 2)


def configure_run(source: Path, destination: Path, port: int, token: str) -> None:
    with source.open('rb') as file:
        data = plistlib.load(file)
    target = data['GPSMotionUITests']
    target['EnvironmentVariables'].update(GPS_STUDIO_PORT=str(port), GPS_STUDIO_TOKEN=token)
    target['TestTimeoutsEnabled'] = False
    with destination.open('wb') as file:
        plistlib.dump(data, file)
    destination.chmod(0o600)


def check_conflict() -> None:
    result = subprocess.run(['pgrep', '-f',
        r'GeoShift.app/Contents/(MacOS/GeoShift|Resources/keeper.py)|xcodebuild.*(GPSMotion|gpsroute-studio-live)'],
        capture_output=True, check=False)
    if result.returncode == 0:
        raise RuntimeError('다른 위치 전송 또는 Xcode 주행이 실행 중입니다. 해당 작업을 먼저 종료하세요.')
    if result.returncode != 1:
        raise RuntimeError('실행 중인 위치 전송을 확인하지 못했습니다.')


async def end_process(process: asyncio.subprocess.Process, grace: float) -> None:
    try:
        await asyncio.wait_for(process.wait(), grace)
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


async def build(project: Path, cache: Path, stop: asyncio.Event) -> bool:
    log = cache / 'build.log'
    emit('status', message='iPhone 전송 준비 중… 처음에는 잠시 걸립니다.')
    with log.open('wb') as output:
        process = await asyncio.create_subprocess_exec(
            '/usr/bin/xcodebuild', '-project', str(project), '-scheme', 'GPSMotion',
            '-configuration', 'Debug', '-destination', 'generic/platform=iOS',
            '-derivedDataPath', str(cache), '-jobs', '1', 'build-for-testing',
            stdout=output, stderr=asyncio.subprocess.STDOUT)
        waiter = asyncio.create_task(process.wait())
        stopper = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait([waiter, stopper], timeout=300, return_when=asyncio.FIRST_COMPLETED)
            if stopper in done or not done:
                await end_process(process, 0)
                if stop.is_set():
                    return False
                raise TimeoutError(f'iPhone 준비 시간이 초과됐습니다. 로그: {log}')
            if waiter.result() != 0:
                raise RuntimeError(f'iPhone 준비에 실패했습니다. Xcode 서명과 연결을 확인하세요. 로그: {log}')
            return True
        finally:
            stopper.cancel()
            with suppress(asyncio.CancelledError):
                await stopper
            if not waiter.done():
                await end_process(process, 0)
            await waiter


async def connect(device, port: int, token: str, process, stop: asyncio.Event) -> MotionConnection | None:
    deadline = time.monotonic() + 180
    while not stop.is_set() and time.monotonic() < deadline:
        if process.returncode is not None:
            raise RuntimeError('iPhone 주행 도우미가 종료됐습니다. 기기 잠금·XCTest 인증 상태를 확인하세요.')
        sock = None
        try:
            sock = await asyncio.wait_for(device.connect(port), 2)
            sock.setblocking(False)
            reader, writer = await asyncio.open_connection(sock=sock, limit=16384)
            return MotionConnection(reader, writer, token)
        except (OSError, asyncio.TimeoutError, PyMobileDevice3Exception):
            if sock is not None:
                sock.close()
            await route.sleep_or_stop(stop, .5)
    if stop.is_set():
        return None
    raise TimeoutError('iPhone 응답이 없습니다. 잠금을 풀고 XCTest 인증 창이 있는지 확인하세요.')


async def main() -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=2_000_000)
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    control = None
    runner = None
    connection = None
    runfile = None
    applied = False
    restored = False
    restore_error = None
    try:
        config = json.loads(await reader.readline())
        playback = Playback(config)
        speed, jitter = settings(config)
        state = dict(speed=speed, jitter=jitter)
        control = asyncio.create_task(commands(reader, state, stop))
        check_conflict()
        devices = [device for device in await asyncio.wait_for(usbmux.list_devices(), 10) if device.is_usb]
        if len(devices) != 1:
            raise RuntimeError('USB로 iPhone 한 대를 연결하고 잠금을 풀어 주세요.')
        device = devices[0]
        root = Path(__file__).resolve().parent
        project = root / 'Motion' / 'GPSReceiver.xcodeproj'
        if not project.exists():
            project = root / 'GPSReceiver.xcodeproj'
        cache = Path(tempfile.gettempdir()) / f'gpsroute-studio-motion-{os.getuid()}'
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stop.is_set() or not await build(project, cache, stop):
            emit('finished', message='전송 준비 취소 · GPS 변경 없음')
            return
        products = cache / 'Build' / 'Products'
        sources = list(products.glob('GPSMotion_*.xctestrun'))
        if len(sources) != 1:
            raise RuntimeError('iPhone 실행 구성을 찾지 못했습니다.')
        port = 40000 + secrets.randbelow(20000)
        token = secrets.token_hex(32)
        runfile = products / 'gpsroute-studio-live.xctestrun'
        configure_run(sources[0], runfile, port, token)
        emit('status', message='iPhone 연결 중… 잠금을 풀어 주세요.')
        with (cache / 'session.log').open('wb') as output:
            runner = await asyncio.create_subprocess_exec(
                '/usr/bin/xcodebuild', '-xctestrun', str(runfile), '-destination', f'id={device.serial}',
                '-parallel-testing-enabled', 'NO', 'test-without-building',
                stdout=output, stderr=asyncio.subprocess.STDOUT)
        connection = await connect(device, port, token, runner, stop)
        if connection is None:
            emit('finished', message='전송 준비 취소 · GPS 변경 없음')
            return
        emit('status', message='iPhone 연결 완료 · 위치와 속도를 함께 전송합니다.')
        started = last_tick = time.monotonic()
        previous = None
        previous_time = None
        while not stop.is_set():
            now = time.monotonic()
            sample = playback.sample(now - last_tick, state)
            last_tick = now
            applied = True  # A lost acknowledgement must still trigger restoration.
            await connection.request('fix', lat=sample['lat'], lon=sample['lon'],
                                     speed=sample['speed'], course=sample['course'])
            sent = time.monotonic()
            target = route.Point(sample['lat'], sample['lon'])
            derived = None if previous is None else route.distance_m(previous, target) / max(.001, sent - previous_time) * 3.6
            emit('sample', **sample, coordinateSpeed=derived, elapsed=sent-started, preview=False, acknowledged=True)
            previous, previous_time = target, sent
            if playback.complete:
                break
            await route.sleep_or_stop(stop, max(0, .5 - (time.monotonic() - now)))
    finally:
        if connection:
            emit('status', message='실제 GPS 복구 확인 중…')
            try:
                await connection.request('stop')
                restored = True
                emit('restored', message='iPhone GPS 시뮬레이션 해제 확인 · 종료 중…')
            except Exception as error:
                restore_error = error
                emit('restore_failed', message=f'GPS 해제 응답을 받지 못했습니다: {error}. 자동 해제 후 폰의 실제 위치를 확인하세요.')
            finally:
                await connection.close()
        if runner:
            # Allow the phone's five-second watchdog and XCTest teardown to finish.
            await end_process(runner, 30 if connection else 1)
            if restored:
                emit('finished', message='GPS 시뮬레이션 해제 완료 · 전송 종료')
            elif applied:
                emit('restore_failed', message='GPS 복구 확인이 필요합니다. 폰에서 실제 위치를 확인하세요.')
        if runfile:
            runfile.unlink(missing_ok=True)
        if control:
            control.cancel()
            with suppress(asyncio.CancelledError):
                await control
        transport.close()
        if restore_error:
            raise RuntimeError('iPhone GPS 해제 확인에 실패했습니다. 실제 위치를 확인하세요.') from restore_error


if __name__ == '__main__':
    try:
        lock_path = Path(tempfile.gettempdir()) / f'gpsroute-studio-{os.getuid()}.lock'
        with lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            asyncio.run(main())
    except Exception as error:
        emit('error', message=f'{type(error).__name__}: {error}')
        sys.exit(1)
