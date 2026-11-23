# SPDX-License-Identifier: Apache-2.0
"""Source text files contain no mojibake: UTF-8 text that was decoded as Windows-1252 and saved again.

Example: "B\u00e9zier" saved through the wrong decoding becomes "B\u00c3\u00a9zier". A place is reported only
when the characters really are the Windows-1252 reading of a UTF-8 sequence. Files that must keep such
characters on purpose (deliberate test data) go in ALLOWED with a reason; an entry whose file no longer
matches fails too, so the list cannot go stale. This file is pure ASCII so that it passes its own check.
"""

import re
import runpy

import pytest

from support.paths import ROOT

pytestmark = [pytest.mark.pure, pytest.mark.cpu]
SOURCE_FILES = runpy.run_path(str(ROOT / "scripts/check-doc-paths.py"))["source_files"]

# path -> reason. Keep this list short and explained. An entry for a file that is not in this tree
# (a private-only file, absent from the public export) is ignored.
ALLOWED = {
    "docs/log/implementation-status.md": "internal work log, kept as written; excluded from the public tree",
}


def _read_as_windows(byte):
    try:
        return bytes([byte]).decode("cp1252")
    except UnicodeDecodeError:
        return chr(byte)  # bytes Windows-1252 leaves undefined come through as Latin-1


# A UTF-8 lead byte (0xC2-0xF4) followed by 1-3 continuation bytes (0x80-0xBF), each read as
# Windows-1252 (or Latin-1) instead of UTF-8.
_LEAD = "".join(chr(byte) for byte in range(0xC2, 0xF5))
_CONTINUATION = "".join(
    sorted({_read_as_windows(byte) for byte in range(0x80, 0xC0)} | {chr(b) for b in range(0x80, 0xC0)})
)
CANDIDATE = re.compile(f"[{re.escape(_LEAD)}][{re.escape(_CONTINUATION)}]{{1,3}}")


def _windows_bytes(text):
    """The bytes that a Windows-1252 (or Latin-1) decoder turned into ``text``."""
    out = bytearray()
    for character in text:
        out.extend(bytes([ord(character)]) if ord(character) < 256 else character.encode("cp1252"))
    return bytes(out)


def mojibake(text):
    """Return (line, excerpt) for every place where text is a wrong decoding of UTF-8."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in CANDIDATE.finditer(line):
            run = match.group(0)
            for end in range(len(run), 1, -1):
                try:
                    repaired = _windows_bytes(run[:end]).decode("utf-8")
                except (UnicodeDecodeError, UnicodeEncodeError):
                    continue
                if repaired != run[:end]:
                    found.append((number, line[max(0, match.start() - 20) : match.end() + 20]))
                    break
    return found


def source_text_files(root=ROOT):
    for name in sorted(SOURCE_FILES(root)):
        path = root / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue  # binary
        yield name, data.decode("utf-8", errors="replace")


@pytest.mark.parametrize(
    "good",
    [
        "B\u00e9zier",
        "\u7247",
        "\u2192",
        "\u00c5ngstr\u00f6m",
        "NVIDIA's",
        "\u00c3 alone",
        "na\u00efve caf\u00e9",
        "\u00b0C",
    ],
)
def test_correct_text_is_not_reported(good):
    assert mojibake(good) == []


@pytest.mark.parametrize("good", ["B\u00e9zier", "\u7247", "\u2192", "\u00fc", "NVIDIA\u2019s", "20\u00b0C"])
def test_wrongly_decoded_text_is_reported(good):
    broken = good.encode("utf-8").decode("cp1252", errors="replace")
    assert broken != good
    assert [line for line, _ in mojibake("ok\n" + broken + "\n")] == [2]
    twice = broken.encode("utf-8").decode("cp1252", errors="replace")
    assert mojibake(twice)


def test_zip_text_inventory_checks_shipped_vendor_text_and_ignores_root_build_outputs(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    bad_text = "B\u00e9zier".encode("utf-8").decode("cp1252")
    nested = "packaging/notices/cycles/third_party/library/NOTICE.txt"
    for name in (nested, "_work/generated.txt", "third_party/source.txt"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(bad_text, encoding="utf-8")
    (tmp_path / "image.bin").write_bytes(b"\0" + bad_text.encode("utf-8"))
    texts = dict(source_text_files(tmp_path))
    assert list(texts) == [nested]
    assert mojibake(texts[nested]) == [(1, bad_text)]


def test_tracked_text_files_have_no_mojibake():
    findings = {}
    for name, text in source_text_files():
        found = mojibake(text)
        if found and name not in ALLOWED:
            findings[name] = found[:5]
    assert not findings, (
        "Text decoded with the wrong encoding (repair it, or add it to ALLOWED with a reason):\n"
        + "\n".join(
            f"{name}:{line}: {excerpt!r}" for name, places in sorted(findings.items()) for line, excerpt in places
        )
    )


def test_allow_list_entries_are_current():
    texts = dict(source_text_files())
    stale = [name for name in ALLOWED if name in texts and not mojibake(texts[name])]
    assert not stale, f"ALLOWED entries no longer needed: {stale}"
