"""Read this project's receiver telemetry over Apple's USB console connection."""
from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import tempfile

from pymobiledevice3 import usbmux

PREFIX = b'GPS_DIAGNOSTIC '


def emit(kind: str, **values) -> None:
    print(json.dumps(dict(kind=kind, **values), ensure_ascii=False), flush=True)


def decode_snapshot(line: bytes) -> dict | None:
    # Only our explicit telemetry prefix is accepted; never parse other app logs.
    if not line.startswith(PREFIX):
        return None
    data = json.loads(line[len(PREFIX):])
    if (not isinstance(data, dict) or data.get('version') != 1
            or type(data.get('active')) is not bool
            or not isinstance(data.get('readings'), dict)
            or not isinstance(data.get('observation'), str)
            or not all(isinstance(k, str) and isinstance(v, str)
                       for k, v in data['readings'].items())):
        raise ValueError('iPhone 진단 응답 형식이 올바르지 않습니다.')
    return data


async def end_process(process) -> None:
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.send_signal(signal.SIGINT)
    try:
        await asyncio.wait_for(process.wait(), 5)
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        await process.wait()


async def run_command(*args: str, timeout: float, log: Path) -> None:
    with log.open('ab') as output:
        process = await asyncio.create_subprocess_exec(*args, stdout=output, stderr=output)
        try:
            async with asyncio.timeout(timeout):
                code = await process.wait()
            if code:
                raise RuntimeError(f'진단 앱 준비에 실패했습니다. iPhone 잠금·연결·개발자 서명을 확인하세요. 로그: {log}')
        except asyncio.TimeoutError as error:
            raise RuntimeError(f'진단 앱 준비 시간이 초과됐습니다. iPhone 연결을 확인하세요. 로그: {log}') from error
        finally:
            await end_process(process)


async def main() -> None:
    devices = [device for device in await asyncio.wait_for(usbmux.list_devices(), 10) if device.is_usb]
    if len(devices) != 1:
        raise RuntimeError('USB로 iPhone 한 대를 연결하고 잠금을 풀어 주세요.')
    serial = devices[0].serial
    root = Path(__file__).resolve().parent
    project = root / 'Motion' / 'GPSReceiver.xcodeproj'
    if not project.exists():
        project = root / 'GPSReceiver.xcodeproj'
    cache = Path(tempfile.gettempdir()) / f'gpsroute-diagnostics-{os.getuid()}'
    cache.mkdir(mode=0o700, exist_ok=True)
    log = cache / 'prepare.log'
    log.write_text('')
    emit('status', message='진단 앱 준비 중… 위치 전송은 계속됩니다.')
    await run_command('/usr/bin/xcodebuild', '-project', str(project), '-scheme', 'GPSDiagnostics',
                      '-configuration', 'Debug', '-destination', 'generic/platform=iOS',
                      '-derivedDataPath', str(cache), '-jobs', '1', 'build', timeout=180, log=log)
    app = cache / 'Build' / 'Products' / 'Debug-iphoneos' / 'GPSReceiver.app'
    await run_command('/usr/bin/xcrun', 'devicectl', 'device', 'install', 'app',
                      '--device', serial, str(app), timeout=60, log=log)
    emit('status', message='iPhone에서 GPS Receiver를 엽니다. 위치·동작 권한을 허용해 주세요.')
    # --console forwards our receiver's stdout, not device-wide system logs.
    with log.open('ab') as errors:
        process = await asyncio.create_subprocess_exec(
            '/usr/bin/xcrun', 'devicectl', 'device', 'process', 'launch',
            '--device', serial, '--terminate-existing', '--console',
            'local.gpsroute.receiver', '--diagnostics',
            stdout=asyncio.subprocess.PIPE, stderr=errors, limit=65536)
        try:
            while True:
                try:
                    line = await asyncio.wait_for(process.stdout.readline(), 8)
                except asyncio.TimeoutError:
                    emit('status', message='새 진단값이 없습니다. iPhone에서 GPS Receiver를 앞에 열어 주세요.')
                    continue
                if not line:
                    code = await process.wait()
                    if code:
                        raise RuntimeError(f'iPhone 진단 연결이 종료됐습니다. 로그: {log}')
                    emit('status', message='iPhone 진단 앱이 종료됐습니다.')
                    return
                snapshot = decode_snapshot(line)
                if snapshot is not None:
                    emit('diagnostic', **snapshot)
        finally:
            # Only this diagnostic receiver is stopped. GPS playback is separate.
            await end_process(process)


async def supervised() -> None:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    previous_signals = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    parent = os.getppid()

    async def watch_parent() -> None:
        while os.getppid() == parent:
            await asyncio.sleep(1)
        task.cancel()

    watcher = asyncio.create_task(watch_parent())
    try:
        await main()
    except asyncio.CancelledError:
        emit('status', message='수신 진단 종료 · 위치 전송 상태는 그대로입니다.')
    except Exception as error:
        emit('error', message=str(error))
        raise SystemExit(1)
    finally:
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher
        for sig, handler in previous_signals.items():
            loop.remove_signal_handler(sig)
            signal.signal(sig, handler)


if __name__ == '__main__':
    asyncio.run(supervised())
