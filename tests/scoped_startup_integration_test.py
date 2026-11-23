# SPDX-License-Identifier: Apache-2.0
"""Houdini startup fixture: studio 123/456/ready and package ready coexist."""

import argparse
import json
import os
from pathlib import Path
import subprocess


parser = argparse.ArgumentParser()
parser.add_argument("--hython", type=Path, required=True)
parser.add_argument("--source", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
output = args.output.resolve()
studio = output / "studio"
scripts = studio / "scripts"
libraries = studio / "python3.13libs"
scripts.mkdir(parents=True, exist_ok=True)
libraries.mkdir(parents=True, exist_ok=True)
events = output / "events.txt"
events.unlink(missing_ok=True)
for path, label in ((scripts / "123.py", "123"), (scripts / "456.py", "456"), (libraries / "ready.py", "ready")):
    path.write_text(
        f"from pathlib import Path\nPath({str(events)!r}).open('a').write({(label + chr(10))!r})\n",
        encoding="utf-8",
    )
probe = output / "probe.py"
probe.write_text(
    "import hou, json\n"
    "import cycles_houdini_offline as offline\n"
    f"path = {str(output / 'ordinary.hiplc')!r}\n"
    "assert offline._installed\n"
    "offline.install()\n"
    "hou.hipFile.save(path, save_to_recent_files=False)\n"
    "hou.hipFile.load(path, suppress_save_prompt=True)\n"
    "print(json.dumps({'installed': offline._installed, "
    "'archive': 'cycles.offline.graphs' in hou.node('/obj').dataBlockKeys(''), "
    "'script_456': hou.findFile('scripts/456.py')}))\n",
    encoding="utf-8",
)
environment = os.environ.copy()
environment.pop("HOUDINI_PACKAGE_DIR", None)
environment.pop("PYTHONPATH", None)
environment["HOUDINI_PACKAGE_SKIPLIST"] = "SideFXLabs22.0.json"
environment["HOUDINI_PATH"] = ";".join((str(studio), str(args.source.resolve() / "houdini"), "&"))
environment["HOUDINI_USER_PREF_DIR"] = str(output / "prefs" / "houdini__HVER__")
process = subprocess.run(
    (str(args.hython.resolve()), str(probe)),
    env=environment,
    cwd=output,
    capture_output=True,
    text=True,
    timeout=45,
)
assert process.returncode == 0, (process.stdout, process.stderr)
result = json.loads(process.stdout.strip().splitlines()[-1])
seen = events.read_text(encoding="utf-8").splitlines()
assert result["installed"] is True and result["archive"] is False, result
assert Path(result["script_456"]).resolve() == (scripts / "456.py").resolve(), result
assert seen.count("ready") == 1 and seen.count("123") == 1, seen
report = dict(result="passed", user_scripts=seen, package_ready=result["installed"], user_456_selected=True)
(output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report))
