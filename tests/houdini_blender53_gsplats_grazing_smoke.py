# SPDX-License-Identifier: Apache-2.0
"""Keep exact-grazing evidence separate from general SH rotation acceptance."""

from pathlib import Path
import subprocess
import hashlib
import json
import re

fixture = Path(__file__).with_name("houdini_blender53_gsplats_smoke.py")
# Reuse only the generated File/Bake/ROP fixture setup, before its test session.
exec(compile(fixture.read_text(encoding="utf-8").split("with ch.Session(")[0], str(fixture), "exec"))
engine = fixture.parents[1] / "third_party/cycles"
upstream = json.loads((fixture.parents[1] / "source.lock.json").read_text(encoding="utf-8"))["cycles"]["commit"]


def function_body(source, name):
    """Compare the relevant C++ function, ignoring braces in comments/strings."""
    start = re.search(r"\b" + re.escape(name) + r"\s*\(", source).start()
    opened = source.index("{", start)
    depth = 0
    for token in re.finditer(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|[{}]', source[opened:]):
        if token.group() == "{":
            depth += 1
        elif token.group() == "}":
            depth -= 1
            if depth == 0:
                return source[opened : opened + token.end()]
    raise AssertionError("Unclosed function " + name)


official = {}
for name, function in (
    ("src/kernel/closure/emissive.h", "emissive_pdf"),
    ("src/kernel/geom/gsplat.h", "gsplat_normal"),
    ("src/kernel/geom/gsplat_intersect.h", "gsplat_intersect_test"),
):
    reference = (
        subprocess.check_output(["git", "-C", str(engine), "show", f"{upstream}:{name}"])
        .decode("utf-8")
        .replace("\r\n", "\n")
    )
    current = (engine / name).read_text(encoding="utf-8")
    wanted = function_body(reference, function)
    actual = function_body(current, function)
    if function == "gsplat_intersect_test":
        # 0084 rejects dead points before upstream math; check the exact guard
        # separately while comparing all remaining intersection math.
        guard = "  if (!point_is_alive(kg, object, prim, time)) {\n    return false;\n  }\n"
        assert actual.count(guard) == 1, "Missing GSplat lifetime rejection"
        actual = actual.replace(guard, "", 1)
    assert actual == wanted, function + " math differs from official pinned upstream"
    official[function] = hashlib.sha256(wanted.encode("utf-8")).hexdigest()
    if function == "emissive_pdf":
        assert "return (cosNI > 0.0f) ? 1.0f : 0.0f;" in wanted
    if function == "gsplat_normal":
        assert "a * a - b * b - c * c + d * d" in wanted
upstream_default = subprocess.check_output(
    ["git", "-C", str(engine), "show", f"{upstream}:src/scene/shader.cpp"]
).decode()
assert "Default Gaussian splat." in upstream_default
default = upstream_default.split("Default Gaussian splat.")[1].split("scene->default_gsplat")[0]
assert "EmissionNode" in default and 'ustring("radiance")' in default

observations = []


def render(session, label):
    frame = session.wait(session.apply(native_scene.capture(rop)))
    image = np.frombuffer(frame["planes"]["combined"]["pixels"], np.float32).reshape(96, 96, 4).copy()
    assert np.isfinite(image).all()
    energy = float(image[:, :, :3].sum())
    np.save(dest / (label + ".npy"), image)
    observations.append(dict(name=label, energy=energy))
    return energy


with ch.Session(device=args.device, width=96, height=96, samples=256, threads=2, use_texture_cache=False) as session:
    assert render(session, "identity-before-grazing") > 1
    transform.parm("ry").set(90)
    choice.parm("input").set(1)
    assert render(session, "live-exact-90-degrees") < 1e-6
    choice.parm("input").set(0)
    assert render(session, "identity-after-grazing") > 1
with ch.Session(device=args.device, width=96, height=96, samples=256, threads=2, use_texture_cache=False) as session:
    choice.parm("input").set(1)
    assert render(session, "initial-exact-90-degrees") < 1e-6
    transform.parm("ry").set(60)
    assert render(session, "live-60-degrees-after-grazing") > 1
report = dict(
    result="passed",
    scope="Exact-grazing renders and comparison of emission PDF, splat normal and intersection math with pinned upstream. The lifetime guard is checked separately.",
    upstream_commit=upstream,
    observations=observations,
    official_kernel_sha256=official,
)
(dest / "report.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report))
