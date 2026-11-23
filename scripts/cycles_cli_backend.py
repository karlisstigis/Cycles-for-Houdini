# SPDX-License-Identifier: Apache-2.0
"""Identify the CPU ray-tracing backend of a standalone Cycles CLI and refuse unlike pairs.

The bias suite compares a pristine ("stock") CLI with the fork CLI. With different BVH
backends (Cycles' own BVH2 versus Embree) the two images differ through last-bit triangle
intersection results, not through engine patches, so such a pair must never be compared.

A CLI whose kernel was compiled with WITH_EMBREE imports embree4.dll; a CLI configured with
the standalone Embree option only (kernel gate off) does not. The record therefore reads the
executable's PE import tables and resolves the Embree runtime it would load through the
Windows search order (executable folder, system folder, then PATH).
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import sys

EMBREE_DLLS = ("embree4.dll", "embree3.dll")


def _section_offset(sections, rva):
    for virtual_address, virtual_size, raw_size, raw_pointer in sections:
        if virtual_address <= rva < virtual_address + max(virtual_size, raw_size):
            return raw_pointer + rva - virtual_address
    raise ValueError(f"RVA {rva:#x} lies outside every section")


def _c_string(data, offset):
    end = data.index(b"\0", offset)
    return data[offset:end].decode("ascii")


def pe_imports(path):
    """Lower-case DLL names from the import and delay-import tables of a PE executable."""
    data = Path(path).read_bytes()
    if data[:2] != b"MZ":
        raise ValueError(f"Not a PE executable: {path}")
    header = struct.unpack_from("<I", data, 0x3C)[0]
    if data[header : header + 4] != b"PE\0\0":
        raise ValueError(f"Missing PE signature: {path}")
    section_count, optional_size = struct.unpack_from("<H12xH", data, header + 6)
    optional = header + 24
    magic = struct.unpack_from("<H", data, optional)[0]
    directories = optional + {0x10B: 96, 0x20B: 112}[magic]
    table = optional + optional_size
    sections = []
    for index in range(section_count):
        virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from("<IIII", data, table + 40 * index + 8)
        sections.append((virtual_address, virtual_size, raw_size, raw_pointer))
    names = []
    import_rva = struct.unpack_from("<I", data, directories + 8 * 1)[0]
    if import_rva:
        cursor = _section_offset(sections, import_rva)
        while True:
            descriptor = struct.unpack_from("<5I", data, cursor)
            if not any(descriptor):
                break
            names.append(_c_string(data, _section_offset(sections, descriptor[3])))
            cursor += 20
    delay_rva = struct.unpack_from("<I", data, directories + 8 * 13)[0]
    if delay_rva:
        cursor = _section_offset(sections, delay_rva)
        while True:
            descriptor = struct.unpack_from("<8I", data, cursor)
            if not any(descriptor):
                break
            names.append(_c_string(data, _section_offset(sections, descriptor[1])))
            cursor += 32
    return [name.lower() for name in names]


def resolve_dll(name, executable, search_path):
    """First match in the executable folder, the Windows system folder, then each PATH entry."""
    folders = [Path(executable).resolve().parent]
    system_root = os.environ.get("SystemRoot")
    if system_root:
        folders.append(Path(system_root) / "System32")
    folders += [Path(entry) for entry in search_path.split(os.pathsep) if entry.strip()]
    for folder in folders:
        candidate = folder / name
        if candidate.is_file():
            return candidate.resolve()
    return None


def describe(executable, search_path):
    """Backend record of one CLI as it would run with ``search_path`` as PATH."""
    executable = Path(executable)
    imports = pe_imports(executable)
    embree = [name for name in imports if name in EMBREE_DLLS]
    record = dict(
        executable=str(executable),
        executable_sha256=hashlib.sha256(executable.read_bytes()).hexdigest(),
        backend="embree" if embree else "bvh2",
        embree_dll=None,
        embree_dll_path=None,
        embree_dll_sha256=None,
    )
    if embree:
        located = resolve_dll(embree[0], executable, search_path)
        if located is None:
            raise FileNotFoundError(f"{executable} imports {embree[0]}, but no such DLL is on its search path")
        record.update(
            embree_dll=embree[0],
            embree_dll_path=str(located),
            embree_dll_sha256=hashlib.sha256(located.read_bytes()).hexdigest(),
        )
    return record


def comparable(stock, fork):
    """Fields that must agree for a stock/fork image comparison to measure engine changes."""
    keys = ("backend", "embree_dll", "embree_dll_sha256")
    return {key: stock.get(key) for key in keys}, {key: fork.get(key) for key in keys}


def require_like_backends(stock, fork):
    if not stock or not fork:
        raise RuntimeError(
            "A bias CLI has no ray-tracing backend record; rebuild stock with scripts/build-stock-cycles.ps1"
            " and recapture so both binaries are identified."
        )
    left, right = comparable(stock, fork)
    if left["backend"] != right["backend"]:
        raise RuntimeError(
            f"Refusing to compare unlike Cycles builds: stock uses {left['backend']}, fork uses {right['backend']}."
            " The difference would measure BVH intersection rounding, not engine patches."
            " Rebuild stock with scripts/build-stock-cycles.ps1 (same Embree as the fork)."
        )
    if left != right:
        raise RuntimeError(
            "Refusing to compare unlike Cycles builds: stock and fork load different Embree runtimes"
            f" ({stock.get('embree_dll_path')} {left['embree_dll_sha256']} versus"
            f" {fork.get('embree_dll_path')} {right['embree_dll_sha256']})."
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("describe", help="Print one CLI's backend record as JSON")
    one.add_argument("--executable", required=True, type=Path)
    one.add_argument("--search-path", default=os.environ.get("PATH", ""))
    pair = commands.add_parser("check", help="Exit nonzero unless stock and fork share one backend")
    pair.add_argument("--stock", required=True, type=Path)
    pair.add_argument("--stock-search-path", required=True)
    pair.add_argument("--fork", required=True, type=Path)
    pair.add_argument("--fork-search-path", required=True)
    pair.add_argument("--stock-receipt", type=Path, help="Stock build receipt whose bvh_backend must match")
    args = parser.parse_args()
    if args.command == "describe":
        print(json.dumps(describe(args.executable, args.search_path)))
        return 0
    stock = describe(args.stock, args.stock_search_path)
    fork = describe(args.fork, args.fork_search_path)
    print(json.dumps(dict(stock=stock, fork=fork)))
    try:
        if args.stock_receipt:
            receipt = json.loads(args.stock_receipt.read_text(encoding="utf-8-sig")).get("bvh_backend")
            if not receipt:
                raise RuntimeError(
                    f"Stock receipt {args.stock_receipt} predates the backend guard (no bvh_backend);"
                    " rebuild stock with scripts/build-stock-cycles.ps1."
                )
            if receipt.get("backend") != stock["backend"]:
                raise RuntimeError("Stock receipt backend differs from the stock executable")
        require_like_backends(stock, fork)
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
