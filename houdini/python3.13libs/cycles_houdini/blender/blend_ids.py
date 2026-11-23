# SPDX-License-Identifier: Apache-2.0
# Ported from Blender v5.3.0 intern/cycles (Apache-2.0, SPDX-FileCopyrightText: 2011-2022 Blender
# Foundation): intern/cycles/util/hash.h (hash_uint2, hash_string), intern/cycles/blender/object.cpp
# (sync_object). Moved here unchanged from the Blender exporter (blender/cycles_blender/blend_export.py),
# so the Houdini side and its tests can compute Object Info > Random without loading Blender code.
"""Cycles' object id hashes, shared by the Blender exporter and the Houdini side (no Blender needed)."""

_UINT = 0xFFFFFFFF


def _rot(value, shift):
    return ((value << shift) | (value >> (32 - shift))) & _UINT


def hash_uint2(kx, ky):
    """Cycles hash_uint2 = Blender BLI_hash_int_2d (Jenkins lookup3 "final" mix)."""
    a = b = c = (0xDEADBEEF + (2 << 2) + 13) & _UINT
    a = (a + (kx & _UINT)) & _UINT
    b = (b + (ky & _UINT)) & _UINT
    c ^= b
    c = (c - _rot(b, 14)) & _UINT
    a ^= c
    a = (a - _rot(c, 11)) & _UINT
    b ^= a
    b = (b - _rot(a, 25)) & _UINT
    c ^= b
    c = (c - _rot(b, 16)) & _UINT
    a ^= c
    a = (a - _rot(c, 4)) & _UINT
    b ^= a
    b = (b - _rot(a, 14)) & _UINT
    c ^= b
    c = (c - _rot(b, 24)) & _UINT
    return c


def hash_string(text):
    """Cycles hash_string = BLI_hash_string over the UTF-8 bytes of a name. Blender is built
    with unsigned `char` (-funsigned-char, MSVC /J), so every byte adds 0..255; checked
    against a Blender 5.3 Cycles render of a non-ASCII name (tests/pytest_cases/
    test_blend_object_values.py)."""
    value = 0
    for byte in text.encode("utf-8"):
        value = (value * 37 + byte) & _UINT
    return value


def object_random_id(name):
    """Object Info > Random of a real object: hash_uint2(hash_string(name), 0) (sync_object)."""
    return hash_uint2(hash_string(name), 0)
