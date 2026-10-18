"""Entry point: python -m fm_tuner  (or FM_Tuner.py)."""

from __future__ import annotations

import argparse
import multiprocessing
import sys

from . import band, servers
from .rtlsdr_device import RtlSdrDevice, RtlSdrError, find_library
from .service import TunerService


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fm_tuner",
        description="RTL-SDR FM tuner with live RDS over localhost TCP.")
    parser.add_argument("-f", "--frequency", default=None,
                        help="Start frequency in MHz on the 0.2 MHz grid "
                             "(87.7-108.1). Default: the last tuned station "
                             "(101.1 on first run)")
    parser.add_argument("-d", "--device", type=int, default=0,
                        help="RTL-SDR device index (default: 0)")
    parser.add_argument("--ppm", type=int, default=0,
                        help="Frequency correction in ppm (default: 0)")
    parser.add_argument("--rds-port", type=int,
                        default=servers.RDS_PORT_PRIMARY,
                        help=f"Primary ASCII G port "
                             f"(default: {servers.RDS_PORT_PRIMARY})")
    parser.add_argument("--legacy-rds-port", type=int,
                        default=servers.RDS_PORT_LEGACY,
                        help=f"Second ASCII G port "
                             f"(default: {servers.RDS_PORT_LEGACY}; "
                             f"0 disables it)")
    parser.add_argument("--telemetry-port", type=int,
                        default=servers.TELEMETRY_PORT,
                        help=f"JSON-lines telemetry port "
                             f"(default: {servers.TELEMETRY_PORT})")
    parser.add_argument("--command-port", type=int,
                        default=servers.COMMAND_PORT,
                        help=f"Command channel port "
                             f"(default: {servers.COMMAND_PORT})")
    parser.add_argument("--no-autostart", action="store_true",
                        help="Open the window without starting reception")
    parser.add_argument("--bias-t", action="store_true",
                        help="Switch the 4.5 V antenna-port bias-tee on at "
                             "start (for a powered LNA or active antenna)")
    parser.add_argument("--no-audio", action="store_true",
                        help="Start with audio output disabled")
    parser.add_argument("--rds2", action="store_true",
                        help="Start with the RDS2 stream decoders "
                             "(66.5/71.25/76 kHz) on; off by default and "
                             "toggled/remembered from the RDS2 tick box")
    parser.add_argument("--list-devices", action="store_true",
                        help="List RTL-SDR and audio output devices, then exit")
    return parser.parse_args(argv)


def list_devices() -> int:
    dll = find_library()
    print(f"rtlsdr library: {dll or 'NOT FOUND'}")
    try:
        names = RtlSdrDevice.device_names()
        if names:
            for index, name in enumerate(names):
                print(f"  RTL-SDR [{index}] {name}")
        else:
            print("  No RTL-SDR devices detected.")
    except RtlSdrError as exc:
        print(f"  {exc}")

    from .receiver import FMReceiver
    devices = FMReceiver.audio_devices()
    if devices:
        print("Audio output devices:")
        for device in devices:
            mark = " (default)" if device["default"] else ""
            print(f"  [{device['index']}] {device['name']}{mark}")
    else:
        print("Audio output devices: none found "
              "(install sounddevice: python -m pip install sounddevice)")
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.list_devices:
        return list_devices()

    try:
        frequency = (band.validate_any(args.frequency)
                     if args.frequency is not None else None)
    except band.FrequencyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    rds_ports = [args.rds_port]
    if args.legacy_rds_port and args.legacy_rds_port != args.rds_port:
        rds_ports.append(args.legacy_rds_port)

    service = TunerService(device_index=args.device,
                           rds_ports=tuple(rds_ports),
                           telemetry_port=args.telemetry_port,
                           command_port=args.command_port,
                           ppm=args.ppm,
                           on_event=lambda kind, msg: print(f"[{kind}] {msg}"))
    if args.no_audio:
        service.receiver.audio_enabled = False
    if args.bias_t:
        service.receiver.set_bias_tee(True)
    if args.rds2:
        service.receiver.set_rds2(True)

    try:
        service.start_servers()
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    from . import gui
    try:
        gui.run(service, frequency, autostart=not args.no_autostart)
    finally:
        service.shutdown()
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
