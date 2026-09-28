"""Android USB transport for GPS Route Studio; standard mock locations stay marked."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import tempfile
import time

from motion_worker import MotionConnection, Playback
from worker import commands, emit, settings
import gpsroute as route

ADB = Path(os.environ.get('ANDROID_SDK_ROOT', str(Path.home() / 'Library/Android/sdk'))) / 'platform-tools/adb'
PACKAGE = 'local.gpsroute.bridge'
RECOVERY_ROOT = Path.home() / 'Library/Application Support/GPS Route Studio/Android Recovery'


def recovery_file(device: str) -> Path:
    return RECOVERY_ROOT / (hashlib.sha256(device.encode()).hexdigest() + '.json')


def save_recovery(device: str, mode: str) -> Path:
    path = recovery_file(device)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as output:
        os.chmod(temporary, 0o600)
        json.dump(dict(device=device, mode=mode), output)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    return path


async def confirm_restoration(device: str) -> None:
    nonce = secrets.token_hex(32)
    result = await adb('shell', 'am', 'start', '-W', '-n', PACKAGE + '/.ControlActivity',
                      '--es', 'action', 'stop', '--es', 'token', nonce, device=device)
    if 'Error:' in result or 'Exception' in result:
        raise RuntimeError('Android 복구 도우미를 열지 못했습니다.')
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        try:
            report = json.loads(await adb('shell', 'run-as', PACKAGE, 'cat', 'files/restore.json', device=device, timeout=2))
        except (RuntimeError, ValueError, asyncio.TimeoutError):
            await asyncio.sleep(.2)
            continue
        if report.get('token') == nonce:
            if report.get('restored') is True:
                return
            raise RuntimeError('Android 위치 복구 실패: ' + str(report.get('error')))
        await asyncio.sleep(.2)
    raise RuntimeError('Android 위치 복구 확인 응답이 없습니다.')


async def recover_previous(device: str) -> None:
    path = recovery_file(device)
    if not path.exists(): return
    saved = json.loads(path.read_text())
    if saved.get('device') != device or saved.get('mode') not in ('allow', 'deny', 'ignore', 'default', 'foreground'):
        raise RuntimeError('이전 Android 복구 기록을 확인해야 합니다.')
    emit('status', message='이전 Android 연결의 위치·권한을 복구하는 중…')
    await adb('shell', 'cmd', 'appops', 'set', PACKAGE, 'MOCK_LOCATION', 'allow', device=device)
    await confirm_restoration(device)
    await adb('shell', 'cmd', 'appops', 'set', PACKAGE, 'MOCK_LOCATION', saved['mode'], device=device)
    path.unlink()


async def adb(*args: str, device: str | None = None, timeout: float = 15) -> str:
    command = [str(ADB)] + (['-s', device] if device else []) + list(args)
    process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        output, error = await asyncio.wait_for(process.communicate(), timeout)
        if process.returncode:
            detail = (error or output).decode(errors='replace').strip()
            raise RuntimeError('Android 연결 명령 실패: ' + detail[-1000:])
        return output.decode(errors='replace').strip()
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


def parse_devices(output: str) -> list[str]:
    return [parts[0] for line in output.splitlines()[1:]
            if len(parts := line.split()) >= 2 and parts[1] == 'device'
            and not parts[0].startswith('emulator-') and ':' not in parts[0]]


def mock_mode(output: str) -> str:
    # Preserve the previous per-package AppOp instead of silently keeping access.
    for line in output.splitlines():
        if 'MOCK_LOCATION:' in line:
            mode = line.split('MOCK_LOCATION:', 1)[1].strip().split(';', 1)[0]
            if mode in ('allow', 'deny', 'ignore', 'default', 'foreground'):
                return mode
    if 'No operations' in output:
        return 'default'
    raise RuntimeError('기존 모의 위치 권한 상태를 확인하지 못했습니다.')


async def main() -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=2_000_000)
    transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    control = None
    device = None
    connection = None
    forwarded = None
    previous_mode = None
    launched = False
    restored = False
    cleanup_error = None
    journal = None
    try:
        config = json.loads(await reader.readline())
        playback = Playback(config)
        speed, jitter = settings(config)
        state = dict(speed=speed, jitter=jitter)
        control = asyncio.create_task(commands(reader, state, stop))
        devices = parse_devices(await adb('devices'))
        if len(devices) != 1:
            raise RuntimeError('USB 디버깅을 허용한 Android 폰 한 대를 연결하세요.')
        device = devices[0]
        sdk = int(await adb('shell', 'getprop', 'ro.build.version.sdk', device=device))
        if sdk < 31:
            raise RuntimeError('Android 12 이상에서 사용할 수 있습니다.')
        allowed = await adb('shell', 'cmd', 'appops', 'query-op', 'android:mock_location', 'allow', device=device)
        other = [line.strip() for line in allowed.splitlines()
                 if line.strip() and line.strip() not in ('No operations.', PACKAGE)]
        if other:
            raise RuntimeError('다른 모의 위치 앱이 선택되어 있습니다. 해당 앱을 먼저 종료하고 선택을 해제하세요.')
        apk = Path(__file__).resolve().parent / 'Android' / 'GPSRouteBridge.apk'
        emit('status', message='Android 전송 도우미 준비 중…')
        await adb('install', '-r', '-g', str(apk), device=device, timeout=60)
        await recover_previous(device)
        if stop.is_set():
            return
        previous_mode = mock_mode(await adb('shell', 'cmd', 'appops', 'get', PACKAGE, 'MOCK_LOCATION', device=device))
        journal = save_recovery(device, previous_mode)
        await adb('shell', 'cmd', 'appops', 'set', PACKAGE, 'MOCK_LOCATION', 'allow', device=device)
        port = 40000 + secrets.randbelow(20000)
        forwarded = int(await adb('forward', 'tcp:0', f'tcp:{port}', device=device))
        token = secrets.token_hex(32)
        launched = True
        result = await adb('shell', 'am', 'start', '-W', '-n', PACKAGE + '/.ControlActivity',
                          '--es', 'action', 'start', '--ei', 'port', str(port), '--es', 'token', token, device=device)
        if 'Error:' in result or 'Exception' in result:
            raise RuntimeError('Android 전송 도우미를 열지 못했습니다: ' + result[-500:])
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not stop.is_set():
            candidate = None
            try:
                rx, tx = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', forwarded, limit=65536), 1)
                candidate = MotionConnection(rx, tx, token)
                await candidate.request('hello')
                connection = candidate
                break
            except (OSError, asyncio.TimeoutError):
                if candidate: await candidate.close()
                await route.sleep_or_stop(stop, .2)
        if connection is None:
            if stop.is_set(): return
            raise RuntimeError('Android 도우미 연결 응답이 없습니다. 폰의 잠금 상태를 확인하세요.')
        emit('status', message='Android 연결 완료 · 모의 위치로 전송합니다.')
        started = last_tick = time.monotonic()
        previous = previous_time = None
        while not stop.is_set():
            now = time.monotonic()
            sample = playback.sample(now - last_tick, state)
            last_tick = now
            reply = await connection.request('fix', **{key: sample[key] for key in ('lat', 'lon', 'speed', 'course')})
            sent = time.monotonic()
            point = route.Point(sample['lat'], sample['lon'])
            derived = None if previous is None else route.distance_m(previous, point) / max(.001, sent - previous_time) * 3.6
            emit('sample', **sample, coordinateSpeed=derived, elapsed=sent-started, acknowledged=True,
                 receivedSpeed=reply.get('receivedSpeed'), receivedMock=reply.get('receivedMock'),
                 receivedAge=reply.get('receivedAge'), preview=False)
            previous, previous_time = point, sent
            if playback.complete: break
            await route.sleep_or_stop(stop, max(0, .5 - (time.monotonic() - now)))
    finally:
        if connection:
            try:
                await connection.request('stop')
                restored = True
                emit('restored', message='Android 모의 위치 해제 확인 · 종료 중…')
            except Exception as error:
                cleanup_error = error
            await connection.close()
        if launched and not restored:
            # The in-service watchdog also restores after a lost USB connection.
            try:
                await confirm_restoration(device)
                restored = True
                cleanup_error = None
            except Exception as error:
                cleanup_error = error
                emit('restore_failed', message='Android 복구를 확인하지 못했습니다. 재연결 후 시작하면 이전 상태를 먼저 복구합니다.')
        if forwarded is not None:
            try: await adb('forward', '--remove', f'tcp:{forwarded}', device=device)
            except Exception as error: cleanup_error = error
        if previous_mode is not None and (not launched or restored):
            try:
                await adb('shell', 'cmd', 'appops', 'set', PACKAGE, 'MOCK_LOCATION', previous_mode, device=device)
                if journal: journal.unlink()
            except Exception as error: cleanup_error = error
        if control:
            control.cancel()
            with suppress(asyncio.CancelledError): await control
        transport.close()
        if cleanup_error:
            raise RuntimeError('Android 정리 상태 확인 필요: ' + str(cleanup_error)) from cleanup_error
        if not launched or restored:
            emit('finished', message='Android 전송 종료 · 모의 위치 변경 없음' if not launched else 'Android 모의 위치 해제 완료')


if __name__ == '__main__':
    try:
        with (Path(tempfile.gettempdir()) / f'gpsroute-studio-{os.getuid()}.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            asyncio.run(main())
    except Exception as error:
        emit('error', message=f'{type(error).__name__}: {error}')
        sys.exit(1)
