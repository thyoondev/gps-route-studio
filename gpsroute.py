#!/usr/bin/env python3
"""Move a paired iPhone's simulated GPS along an interpolated route.

Uses the same pymobiledevice3 developer-tunnel path as GeoShift, but streams
interpolated coordinates at a fixed cadence instead of holding one point.

Install the tested Python environment with
    uv tool install --python 3.13 'pymobiledevice3==9.31.0'
Run this script with that environment’s Python interpreter.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from pymobiledevice3.exceptions import NoDeviceConnectedError
from pymobiledevice3.remote import tunnel_service, userspace_tunnel
from pymobiledevice3.remote.tunnel_service import iter_remote_paired_identifiers
from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
from pymobiledevice3.usbmux import list_devices

EARTH_RADIUS_M = 6_371_008.8
METERS_PER_DEGREE_LAT = 111_320.0
CONNECT_TIMEOUT_SECONDS = 30
SET_TIMEOUT_SECONDS = 5
REMOTE_BONJOUR_TIMEOUT_SECONDS = 12
DEFAULT_SPEED_KMH = 4.5
DEFAULT_INTERVAL_SECONDS = 1.0
MIN_CENTRAL_ANGLE = 1e-12


class RouteError(ValueError):
    """The requested route could not be built from the given input."""


class AmbiguousDeviceError(RuntimeError):
    """More than one candidate iPhone is visible."""


@dataclass(frozen=True)
class Point:
    lat: float
    lon: float

    def __str__(self) -> str:
        return f"{self.lat:.6f},{self.lon:.6f}"


# --------------------------------------------------------------------------- geometry


def central_angle(a: Point, b: Point) -> float:
    """Great-circle angular distance in radians (haversine)."""
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlon = math.radians(b.lon - a.lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * math.asin(math.sqrt(min(1.0, h)))


def distance_m(a: Point, b: Point) -> float:
    return central_angle(a, b) * EARTH_RADIUS_M


def interpolate(a: Point, b: Point, fraction: float) -> Point:
    """Point at `fraction` along the great circle from a to b (slerp)."""
    d = central_angle(a, b)
    if d < MIN_CENTRAL_ANGLE:
        return a

    lat1, lon1 = math.radians(a.lat), math.radians(a.lon)
    lat2, lon2 = math.radians(b.lat), math.radians(b.lon)
    wa = math.sin((1 - fraction) * d) / math.sin(d)
    wb = math.sin(fraction * d) / math.sin(d)

    x = wa * math.cos(lat1) * math.cos(lon1) + wb * math.cos(lat2) * math.cos(lon2)
    y = wa * math.cos(lat1) * math.sin(lon1) + wb * math.cos(lat2) * math.sin(lon2)
    z = wa * math.sin(lat1) + wb * math.sin(lat2)
    return Point(math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x)))


def jittered(point: Point, meters: float) -> Point:
    """Gaussian positional noise, in meters, to mimic real GPS scatter."""
    if meters <= 0:
        return point
    north = random.gauss(0.0, meters)
    east = random.gauss(0.0, meters)
    lat = point.lat + north / METERS_PER_DEGREE_LAT
    lon_scale = METERS_PER_DEGREE_LAT * max(0.01, math.cos(math.radians(point.lat)))
    return Point(lat, point.lon + east / lon_scale)


# --------------------------------------------------------------------------- route


def parse_point(text: str) -> Point:
    parts = text.replace(" ", "").split(",")
    if len(parts) != 2:
        raise RouteError(f"Expected 'lat,lon' but got {text!r}")
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError as error:
        raise RouteError(f"Non-numeric coordinate in {text!r}") from error
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise RouteError(f"Coordinate out of range: {text!r}")
    return Point(lat, lon)


def read_gpx(path: Path) -> list[Point]:
    root = ET.parse(path).getroot()
    tags = ("trkpt", "rtept", "wpt")
    points = [
        Point(float(node.attrib["lat"]), float(node.attrib["lon"]))
        for node in root.iter()
        if node.tag.rsplit("}", 1)[-1] in tags and "lat" in node.attrib
    ]
    if not points:
        raise RouteError(f"No trkpt/rtept/wpt elements found in {path}")
    return points


def build_waypoints(args: argparse.Namespace) -> list[Point]:
    points = read_gpx(Path(args.gpx)) if args.gpx else [parse_point(p) for p in args.point]
    if len(points) < 2:
        raise RouteError("A route needs at least two waypoints")
    if args.reverse:
        points = list(reversed(points))
    if args.pingpong:
        points = points + list(reversed(points))[1:]
    return points


def leg_samples(a: Point, b: Point, step_m: float) -> list[Point]:
    """Interpolated points strictly after `a`, ending exactly on `b`."""
    span = distance_m(a, b)
    if span < step_m / 2:
        return [b]
    steps = max(1, math.ceil(span / step_m))
    return [interpolate(a, b, i / steps) for i in range(1, steps + 1)]


def build_track(waypoints: list[Point], step_m: float, hold_ticks: int) -> list[Point]:
    """Full ordered sample list, including repeated points for waypoint holds."""
    track = [waypoints[0]] + [waypoints[0]] * hold_ticks
    for a, b in zip(waypoints, waypoints[1:]):
        track.extend(leg_samples(a, b, step_m))
        track.extend([b] * hold_ticks)
    return track


def route_length_m(waypoints: list[Point]) -> float:
    return sum(distance_m(a, b) for a, b in zip(waypoints, waypoints[1:]))


# --------------------------------------------------------------------------- device


async def resolve_target_device(saved_udid: str | None) -> tuple[str, bool]:
    """Return (udid, uses_remote_pairing), mirroring GeoShift's resolution order."""
    devices = await list_devices()
    usb_udids = sorted({device.serial for device in devices})

    if saved_udid:
        if saved_udid in usb_udids:
            return saved_udid, False
        if saved_udid in set(iter_remote_paired_identifiers()):
            return saved_udid, True
        raise NoDeviceConnectedError()

    if usb_udids:
        if len(usb_udids) > 1:
            raise AmbiguousDeviceError("Several iPhones are visible over USB; pass --udid.")
        return usb_udids[0], False

    paired = sorted(set(iter_remote_paired_identifiers()))
    if not paired:
        raise NoDeviceConnectedError()
    if len(paired) > 1:
        raise AmbiguousDeviceError("Several remotely paired iPhones are available; pass --udid.")
    return paired[0], True


async def open_target_tunnel(target_udid: str, uses_remote_pairing: bool):
    tunnel = UserspaceRsdTunnel(serial=target_udid, autopair=not uses_remote_pairing)
    if not uses_remote_pairing:
        try:
            return tunnel, await asyncio.wait_for(tunnel.aopen(), timeout=CONNECT_TIMEOUT_SECONDS)
        except BaseException:
            with suppress(Exception):
                await asyncio.wait_for(tunnel.aclose(), timeout=SET_TIMEOUT_SECONDS)
            raise

    original_factory = userspace_tunnel._create_no_root_tunnel_provider

    async def remote_pairing_provider(serial: str | None, autopair: bool):
        services = await tunnel_service.get_remote_pairing_tunnel_services(
            bonjour_timeout=REMOTE_BONJOUR_TIMEOUT_SECONDS,
            udid=serial,
        )
        if not services:
            raise NoDeviceConnectedError()
        return services[0], None

    # Same seam GeoShift patches: the public userspace tunnel probes usbmux
    # before its Wi-Fi fallback, and a device-initiated pair has no usbmux entry.
    userspace_tunnel._create_no_root_tunnel_provider = remote_pairing_provider
    try:
        rsd = await asyncio.wait_for(tunnel.aopen(), timeout=CONNECT_TIMEOUT_SECONDS)
        return tunnel, rsd
    except BaseException:
        with suppress(Exception):
            await asyncio.wait_for(tunnel.aclose(), timeout=SET_TIMEOUT_SECONDS)
        raise
    finally:
        userspace_tunnel._create_no_root_tunnel_provider = original_factory


def geoshift_is_running() -> bool:
    result = subprocess.run(
        ["pgrep", "-f", "GeoShift.app/Contents/MacOS/GeoShift"],
        capture_output=True,
    )
    return result.returncode == 0


# --------------------------------------------------------------------------- run


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, but wake immediately if a stop was requested."""
    if seconds <= 0:
        return
    with suppress(asyncio.TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def send_track(
    location: LocationSimulation,
    track: list[Point],
    args: argparse.Namespace,
    stop: asyncio.Event,
) -> None:
    """Push each sample on a monotonic schedule so pacing does not drift."""
    start = time.monotonic()
    total = len(track)

    for index, point in enumerate(track):
        if stop.is_set():
            return
        target = jittered(point, args.jitter)
        await asyncio.wait_for(location.set(target.lat, target.lon), timeout=SET_TIMEOUT_SECONDS)

        elapsed = time.monotonic() - start
        print(f"\r[{index + 1:>5}/{total}] {target}  t={elapsed:6.1f}s", end="", flush=True)

        if index + 1 < total:
            await sleep_or_stop(stop, start + (index + 1) * args.interval - time.monotonic())
    print()


def install_stop_handlers(stop: asyncio.Event) -> None:
    """Turn Ctrl-C into a cooperative stop so the clear still gets sent."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


async def restore_gps(location: LocationSimulation, args: argparse.Namespace) -> None:
    if args.keep:
        print("Leaving the last simulated point active (--keep).")
        return
    try:
        # Shielded: a cancelled context must not abort the restore.
        await asyncio.shield(asyncio.wait_for(location.clear(), timeout=SET_TIMEOUT_SECONDS))
        print("Simulation cleared. Open Maps to confirm the real fix returns.")
    except Exception as error:
        print(f"WARNING: clear failed ({error}). The iPhone may still report a fake "
              f"location -- open GeoShift and press Restore GPS.", file=sys.stderr)


async def drive_route(track: list[Point], args: argparse.Namespace) -> int:
    udid, remote = await resolve_target_device(args.udid)
    print(f"Target iPhone {udid} ({'Wi-Fi pairing' if remote else 'USB'})")

    stop = asyncio.Event()
    install_stop_handlers(stop)

    tunnel = None
    try:
        tunnel, rsd = await open_target_tunnel(udid, remote)
        if rsd.udid != udid:
            raise RuntimeError(f"Connected to unexpected iPhone {rsd.udid}; expected {udid}")

        async with DvtProvider(rsd) as dvt, LocationSimulation(dvt) as location:
            print("Developer tunnel open. Ctrl-C stops and restores real GPS.\n")
            try:
                laps = 0
                while not stop.is_set() and (args.laps == 0 or laps < args.laps):
                    await send_track(location, track, args, stop)
                    laps += 1
                if stop.is_set():
                    print("\nStopping.")
            finally:
                await restore_gps(location, args)
        return 130 if stop.is_set() else 0
    finally:
        if tunnel is not None:
            with suppress(Exception):
                await asyncio.wait_for(tunnel.aclose(), timeout=SET_TIMEOUT_SECONDS)


# --------------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Move a paired iPhone's simulated GPS along an interpolated route.",
        epilog="Example: gpsroute.py -p 37.5665,126.9780 -p 37.5512,126.9882 --speed 4.5",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("-p", "--point", action="append", metavar="LAT,LON",
                        help="waypoint; repeat for each corner of the route")
    source.add_argument("--gpx", metavar="FILE", help="GPX file to read waypoints from")

    parser.add_argument("--speed", type=float, default=DEFAULT_SPEED_KMH,
                        help=f"travel speed in km/h (default {DEFAULT_SPEED_KMH}, walking)")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS,
                        help=f"seconds between updates (default {DEFAULT_INTERVAL_SECONDS})")
    parser.add_argument("--hold", type=float, default=0.0,
                        help="seconds to pause at each waypoint (default 0)")
    parser.add_argument("--jitter", type=float, default=0.0,
                        help="gaussian position noise in meters (default 0, off)")
    parser.add_argument("--laps", type=int, default=1,
                        help="times to run the route; 0 repeats forever (default 1)")
    parser.add_argument("--pingpong", action="store_true",
                        help="append the reversed route so it returns to the start")
    parser.add_argument("--reverse", action="store_true", help="walk the waypoints backwards")
    parser.add_argument("--keep", action="store_true",
                        help="leave the final point simulated instead of restoring real GPS")
    parser.add_argument("--udid", help="target a specific iPhone when several are paired")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and the first samples without connecting")
    parser.add_argument("--force", action="store_true",
                        help="run even while GeoShift is open (they will fight over the location)")
    return parser


def validate(args: argparse.Namespace) -> None:
    if args.speed <= 0:
        raise RouteError("--speed must be greater than 0")
    if args.interval <= 0:
        raise RouteError("--interval must be greater than 0")
    if args.hold < 0:
        raise RouteError("--hold cannot be negative")
    if args.laps < 0:
        raise RouteError("--laps cannot be negative")


def print_plan(waypoints: list[Point], track: list[Point], args: argparse.Namespace) -> None:
    length = route_length_m(waypoints)
    duration = len(track) * args.interval
    step = args.speed * 1000 / 3600 * args.interval
    print(f"Waypoints : {len(waypoints)}")
    print(f"Distance  : {length:,.0f} m ({length / 1000:.2f} km)")
    print(f"Speed     : {args.speed:g} km/h  ->  {step:.2f} m per {args.interval:g}s update")
    print(f"Samples   : {len(track)}  (~{duration / 60:.1f} min per lap)")
    print(f"Laps      : {'infinite' if args.laps == 0 else args.laps}")
    if args.jitter:
        print(f"Jitter    : {args.jitter:g} m gaussian")
    print()


def main() -> int:
    args = build_parser().parse_args()
    try:
        validate(args)
        waypoints = build_waypoints(args)
        step_m = args.speed * 1000 / 3600 * args.interval
        track = build_track(waypoints, step_m, round(args.hold / args.interval))
    except RouteError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    print_plan(waypoints, track, args)

    if args.dry_run:
        for point in track[:10]:
            print(f"  {point}")
        if len(track) > 10:
            print(f"  ... {len(track) - 10} more")
        return 0

    if geoshift_is_running() and not args.force:
        print("error: GeoShift is running and will keep re-applying its own fixed point.",
              file=sys.stderr)
        print("       Quit GeoShift first, or pass --force.", file=sys.stderr)
        return 1

    try:
        return asyncio.run(drive_route(track, args))
    except KeyboardInterrupt:
        return 130
    except NoDeviceConnectedError:
        print("error: no paired iPhone found. Connect by USB or finish Wi-Fi pairing.",
              file=sys.stderr)
        return 1
    except AmbiguousDeviceError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
