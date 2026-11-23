# SPDX-License-Identifier: Apache-2.0
"""Copied protected material graphs get their own identity; genuine conflicts still need review.

Single hython process; every file is written below --output (a temporary folder).
Covers: copy, Alt-drag (copyItems) and clipboard paste in one scene, paste from
another scene, copying a network that holds several protected graphs, repair on
load of duplicates saved by earlier versions (nothing written until save, quiet
after one save), the original keeping its identity and archive record, a genuine
structural conflict still being reported, Keep Current Graph clearing it, and a
save/reload round trip without notices.
"""

import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import uuid
import zipfile

import hou
import cycles_houdini as ch
import cycles_houdini_offline as protection

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
MARKER = protection.MARKER
checks = []


def check(name, condition, detail=None):
    checks.append(dict(name=name, ok=bool(condition), detail=detail))
    print(
        ("ok: " if condition else "FAIL: ") + name + ("" if detail is None else " " + json.dumps(detail, default=str))
    )


def notices(action):
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        action()
    lines = [line for line in stream.getvalue().splitlines() if line.startswith("Cycles scene protection:")]
    print("\n".join("    " + line for line in lines))
    return lines


def load(path):
    return notices(lambda: hou.hipFile.load(str(path), suppress_save_prompt=True))


def save(path):
    return notices(lambda: hou.hipFile.save(str(path), save_to_recent_files=False))


def identity(node):
    return node.userData(MARKER)


def canonical(value):
    try:
        return str(uuid.UUID(value)) == value
    except (TypeError, ValueError):
        return False


def records():
    return protection._read()[0]


def statuses():
    return {row["path"]: row["status"] for row in protection.inspect()}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


pending = []
# Copy events are watched in the UI only. hython has no UI event loop: watch anyway
# and run the deferred check by hand, as the UI does after the paste completes.
protection._watch_copies = True
protection._defer = pending.append


def run_deferred():
    calls = list(pending)
    pending.clear()
    for callback in calls:
        callback()
    return len(calls)


def paste(network):
    hou.pasteNodesFromClipboard(network)
    return hou.selectedNodes()[0]


def paste_copy_of(node):
    hou.copyNodesToClipboard((node,))
    return paste(node.parent())


def archived_payload(block, key):
    with zipfile.ZipFile(io.BytesIO(block)) as archive:
        return archive.read(key + ".cpio")


# 1. Copies within one scene, at the moment of the copy.
hou.hipFile.clear(suppress_save_prompt=True)
original = ch.create_material(name="original")
base = args.output / "base.hiplc"
check("first save is quiet", save(base) == [])
first = identity(original)
check("first save gives the original an identity", canonical(first), first)
for label, copy_action in (
    ("copy (copyNodesTo)", lambda: hou.copyNodesTo((original,), hou.node("/mat"))[0]),
    ("Alt-drag (copyItems)", lambda: hou.node("/mat").copyItems((original,))[0]),
    ("clipboard paste", lambda: paste_copy_of(original)),
):
    copy = copy_action()
    check(f"{label}: Houdini copies the hidden identity with the node", identity(copy) == first)
    check(f"{label}: one deferred check is scheduled by the copy", run_deferred() == 1)
    check(f"{label}: the copy has its own identity", canonical(identity(copy)) and identity(copy) != first)
    check(f"{label}: the original keeps its identity", identity(original) == first)
within = args.output / "within.hiplc"
check("saving after copies is quiet", save(within) == [])
saved = records()
check("the original keeps its archive record and path", saved[first]["path"] == "/mat/original")
check("each copy has its own archive record", len(saved) == 4, sorted(r["path"] for r in saved.values()))
check("all graphs healthy", all(s.startswith("healthy") for s in statuses().values()), statuses())

# 2. Paste from another scene.
other = args.output / "other.hiplc"
hou.hipFile.clear(suppress_save_prompt=True)
ch.create_material(name="unrelated")
save(other)
load(within)
hou.copyNodesToClipboard((hou.node("/mat/original"),))
load(other)
pasted = paste(hou.node("/mat"))
run_deferred()
check("paste into a scene without the original keeps its identity (unique there)", identity(pasted) == first)
load(within)  # the same scene, which already holds the original
pasted = paste(hou.node("/mat"))
run_deferred()
check("paste from another session into the scene holding the original: copy separated", identity(pasted) != first)
check("  original untouched", identity(hou.node("/mat/original")) == first)

# 3. Copying a network that holds several protected graphs, and a selection of all of /mat.
hou.hipFile.clear(suppress_save_prompt=True)
box = hou.node("/obj").createNode("matnet", "box")
inner = [ch.create_material(parent=box, name=f"inner{index}") for index in range(3)]
save(args.output / "container.hiplc")
inner_ids = [identity(node) for node in inner]
box_copy = hou.copyNodesTo((box,), hou.node("/obj"))[0]
check("container copy: one deferred check", run_deferred() == 1)
copies = [box_copy.node(node.name()) for node in inner]
check(
    "container copy: every nested copy has a new identity",
    all(canonical(identity(c)) and identity(c) not in inner_ids for c in copies),
)
check("container copy: nested copies are distinct", len({identity(c) for c in copies}) == 3)
check("container copy: originals keep theirs", [identity(node) for node in inner] == inner_ids)
mats = [ch.create_material(name=f"m{index}") for index in range(2)]
save(args.output / "container.hiplc")
mat_ids = [identity(node) for node in mats]
mat_copies = hou.copyNodesTo(tuple(mats), hou.node("/mat"))
run_deferred()
check(
    "copy of several /mat graphs at once: originals keep, copies new",
    [identity(n) for n in mats] == mat_ids and not {identity(n) for n in mat_copies} & set(mat_ids),
)
check("container scene saves quietly", save(args.output / "container.hiplc") == [])

# 4. Duplicates saved by an earlier version repair themselves on load.
protection._defer = lambda callback: None  # like the earlier version: copies keep the identity
hou.hipFile.clear(suppress_save_prompt=True)
threads = ch.create_material(name="Threads")
save(args.output / "legacy-base.hiplc")
legacy = identity(threads)
legacy_copy = hou.copyNodesTo((threads,), hou.node("/mat"))[0]
# Like /mat/Threads: edits after the copy never reached the frozen archive.
extra = threads.createNode("cycles::emission::1", "added_after_copy")
# The default this test was written for (new nodes now take Blender's).
extra.setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
threads.node("surface_output").setInput(0, extra)
protection._busy = True  # the earlier version refused to refresh duplicated identities
try:
    hou.hipFile.save(str(args.output / "legacy-duplicates.hiplc"), save_to_recent_files=False)
finally:
    protection._busy = False
protection._defer = pending.append
check("fixture: duplicate identity saved", identity(legacy_copy) == legacy)
legacy_file = args.output / "legacy-duplicates.hiplc"
before = sha(legacy_file)
lines = load(legacy_file)
check("load repairs with one short line", len(lines) == 1 and "/mat/Threads1" in lines[0], lines)
check("the node at the archived path keeps the identity", identity(hou.node("/mat/Threads")) == legacy)
check("the copy has its own identity", identity(hou.node("/mat/Threads1")) not in (None, legacy))
check("nothing waits for review", not protection._unresolved, sorted(protection._unresolved))
check("nothing written to disk by loading", sha(legacy_file) == before)
check("an unsaved reload repairs again (same one line)", len(load(legacy_file)) == 1)
repaired = args.output / "legacy-repaired.hiplc"
check("first save after repair is quiet", save(repaired) == [])
check(
    "archive refreshed with the authored additions",
    any(r["path"] == "added_after_copy" for r in records()[legacy]["nodes"]),
)
check("reload after one save: no notices", load(repaired) == [])
check("reload: all healthy", all(s.startswith("healthy") for s in statuses().values()), statuses())
check("reload: identities kept", identity(hou.node("/mat/Threads")) == legacy)
review = hou.node("/obj").createNode("matnet", "review")
recovered = protection.recover_copy(legacy, review)
check("archive of the original is valid and recoverable", recovered.node("added_after_copy") is not None)
review.destroy()

# 5. A genuine conflict is still reported, also when a copy exists; Keep Current Graph clears it.
hou.hipFile.clear(suppress_save_prompt=True)
damaged = ch.create_material(name="structural_loss", shader="emission")
# The default this test was written for (new nodes now take Blender's).
damaged.node("surface_shader").setParms(dict(color=(0.8, 0.8, 0.8), strength=10.0))
save(args.output / "intact.hiplc")
lost = identity(damaged)
intact = hou.node("/obj").dataBlock(protection.BLOCK)
protection._defer = lambda callback: None
hou.copyNodesTo((damaged,), hou.node("/mat"))
damaged.node("surface_shader").destroy()
protection._busy = True  # as if a child vanished while loading and the scene was saved
try:
    hou.hipFile.save(str(args.output / "damaged.hiplc"), save_to_recent_files=False)
finally:
    protection._busy = False
protection._defer = pending.append
lines = load(args.output / "damaged.hiplc")
check(
    "load: copy separated and the lost node reported, one line each",
    len(lines) == 2
    and "/mat/structural_loss1" in lines[0]
    and "/mat/structural_loss:" in lines[1]
    and "Keep Current Graph" in lines[1],
    lines,
)
damaged = hou.node("/mat/structural_loss")
check("genuine conflict waits for review", lost in protection._unresolved)
check("menu item shown on it", protection.needs_review(dict(node=damaged)))
check("menu item hidden on its copy", not protection.needs_review(dict(node=hou.node("/mat/structural_loss1"))))
check("menu item hidden without a node", not protection.needs_review({}))
check("save before review keeps the archive", save(args.output / "damaged-resaved.hiplc") == [])
check("  archive record unchanged", protection._read()[1][lost] == archived_payload(intact, lost))
lines = load(args.output / "damaged-resaved.hiplc")
check("still reported at the next load (not resolved)", len(lines) == 1 and "Keep Current Graph" in lines[0], lines)
damaged = hou.node("/mat/structural_loss")
check("Keep Current Graph accepts it", protection.keep_current(dict(node=damaged)) is True)
check("  no longer waiting for review", lost not in protection._unresolved)
check("  menu item hidden again", not protection.needs_review(dict(node=damaged)))
accepted = args.output / "accepted.hiplc"
save(accepted)
check("  archive now holds the current graph", not any(r["path"] == "surface_shader" for r in records()[lost]["nodes"]))
check("round trip after accepting: no notices", load(accepted) == [])
check("round trip: all healthy", all(s.startswith("healthy") for s in statuses().values()), statuses())

failed = [c for c in checks if not c["ok"]]
result = dict(result="failed" if failed else "passed", checks=len(checks), failed=failed)
(args.output / "copies.json").write_text(json.dumps(result, indent=2, default=str))
print("RESULT", "FAIL" if failed else "PASS", len(checks), "checks")
raise SystemExit(1 if failed else 0)
