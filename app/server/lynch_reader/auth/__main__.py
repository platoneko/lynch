"""Pair, list and revoke the app server's devices: `python -m lynch_reader.auth pair|devices|revoke`.

The server has no admin endpoints; this CLI on the host is the only way to manage devices.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from lynch_reader.auth.store import CODE_TTL_S, DEVICES_FILE, DeviceStore, StoreCorrupt
from lynch_reader.web import DEFAULT_PORT

# MagicDNS name of this host: what a laptop or phone on the tailnet opens.
PAIR_HOST = "pivot-home.tail090ab2.ts.net"


def pair(store: DeviceStore, args: argparse.Namespace) -> int:
    name = args.name or datetime.now().strftime("设备 %Y-%m-%d %H:%M")
    code = store.create_pairing(name)
    print(f"配对码：{code}（{int(CODE_TTL_S // 60)} 分钟内有效，只能用一次）")
    print(f"设备名：{name}")
    print(f"浏览器打开：http://{PAIR_HOST}:{DEFAULT_PORT}/pair?code={code}")
    return 0


def devices(store: DeviceStore, args: argparse.Namespace) -> int:
    rows = store.devices()
    if not rows:
        print("还没有配对的设备")
    for d in rows:
        print(f"{d.id}  {d.paired_at[:16].replace('T', ' ')}  {d.name}")
    return 0


def revoke(store: DeviceStore, args: argparse.Namespace) -> int:
    if not store.revoke(args.id):
        print(f"没有这个设备：{args.id}", file=sys.stderr)
        return 1
    print(f"已吊销 {args.id}")
    return 0


def main(argv: list[str] | None = None, store: DeviceStore | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m lynch_reader.auth")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pair", help="新的一次性配对码（替换未用的旧码）")
    p.add_argument("name", nargs="?", default="", help="设备名，省略时用当前时间")
    p.set_defaults(run=pair)
    sub.add_parser("devices", help="列出已配对设备").set_defaults(run=devices)
    r = sub.add_parser("revoke", help="吊销一台设备，立即生效")
    r.add_argument("id")
    r.set_defaults(run=revoke)
    args = ap.parse_args(argv)
    try:
        return args.run(store or DeviceStore(DEVICES_FILE), args)
    except StoreCorrupt as e:
        print(f"设备文件损坏，未做任何修改，请手工处理：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
