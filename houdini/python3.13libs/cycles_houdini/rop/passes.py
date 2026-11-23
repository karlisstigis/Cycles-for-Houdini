# SPDX-License-Identifier: Apache-2.0
"""Authored Houdini labels/semantics over the compiled, certified pass catalogue."""

from .. import _native
from .rop_controls import value

MANAGED = set()
LABELS = {
    "combined": "Beauty",
    "depth": "Depth",
    "normal": "Normal",
    "uv": "UV",
    "position": "Position",
    "roughness": "Roughness",
    "mist": "Mist",
    "sample_count": "Sample Count",
    "ao": "Ambient Occlusion",
    "object_id": "Object Index",
    "material_id": "Material Index",
    "shadow_catcher": "Shadow Catcher",
    "motion": "Motion Vectors",
    "denoising_depth": "Denoising Depth",
    "background": "Environment",
    "emission": "Emission",
    "noisy_combined": "Beauty (Noisy)",
    "denoising_albedo": "Denoising Albedo (Raw)",
    "denoising_normal": "Denoising Normal (Raw)",
}


def label(name):
    for prefix, title in (("CryptoObject", "Object"), ("CryptoMaterial", "Material"), ("CryptoAsset", "Asset")):
        if name.startswith(prefix):
            return "Cryptomatte " + title + " " + name[len(prefix) :]
    if name.startswith("lightgroup_"):
        return "Light Group: " + name[len("lightgroup_") :]
    if name in LABELS:
        return LABELS[name]
    return name.replace("_", " ").title() if name in {p["id"] for p in _native.pass_catalogue()} else name


SPECIAL = {"crypto_object", "crypto_material", "crypto_asset", "aov_color", "aov_value", "lightgroup"}


def entries(rop, at_time, *, output=False):
    result = []
    separate_all = bool(value(rop, "output_separate_passes", at_time)) if output else False
    multipart = output and not separate_all and bool(value(rop, "output_multipart", at_time))
    descriptors = catalogue()
    known = {entry["id"] for entry in descriptors} - {"combined"} | SPECIAL
    for index in range(1, int(value(rop, "pass_count", at_time)) + 1):

        def get(name):
            return value(rop, "pass_" + name + str(index), at_time)

        if not get("enable"):
            continue
        kind = get("type")
        if kind not in known:
            raise ValueError("Unknown pass type: " + kind)
        if kind == "motion" and value(rop, "allowmotionblur", at_time):
            continue
        source = get("source") if kind in ("aov_color", "aov_value", "lightgroup") else kind
        if not source:
            raise ValueError("Set a source name for pass " + str(index))
        default = (
            ("Crypto" + kind[7:].title())
            if kind.startswith("crypto_")
            else ("lightgroup_" + source if kind == "lightgroup" else source)
        )
        name = (get("name").strip() or default) if output else default
        if output and (
            not name.isascii() or not name.replace("_", "a").isalnum() or not (name[0].isalpha() or name[0] == "_")
        ):
            raise ValueError("Pass layer names must contain ASCII letters, digits and underscores: " + name)
        row = dict(
            index=index,
            type=kind,
            source=source,
            name=name,
            levels=int(get("levels")) if kind.startswith("crypto_") else 0,
        )
        if output:
            separate = not multipart and (separate_all or bool(get("separate")))
            codec = get("compression") if separate or multipart else "inherit"
            row.update(
                separate=separate,
                path=get("path") if separate else "",
                precision=get("precision"),
                compression=codec,
                dwa_level=get("dwa_level") if codec in ("dwaa", "dwab") else 45.0,
            )
        result.append(row)
    return result


def add(rop, kind, **settings):
    """Author a real native multiparm entry; shared by shelf tools and fixtures."""
    count = rop.parm("pass_count")
    index = count.evalAsInt() + 1
    count.set(index)
    values = {"type": kind, **settings}
    rop.setParms({"pass_" + name + str(index): value for name, value in values.items()})
    return index


def has(rop, kind, at_time):
    if kind == "motion" and value(rop, "allowmotionblur", at_time):
        return False
    return any(
        value(rop, "pass_enable" + str(index), at_time) and value(rop, "pass_type" + str(index), at_time) == kind
        for index in range(1, int(value(rop, "pass_count", at_time)) + 1)
    )


def menu():
    import cycles_houdini_startup as startup

    descriptors = []
    # Houdini evaluates menus while loading a HIP, before or after OnLoaded.
    # Reuse the automatic-startup latch so an unavailable SDK is diagnosed once
    # and the ROP stays editable. Explicit catalogue/render calls still raise.
    if not startup._run(lambda: descriptors.extend(catalogue())):
        return []
    result = []
    lighting = ("diffuse_", "glossy_", "transmission_", "volume_")
    for entry in descriptors:
        kind = entry["id"]
        if kind == "combined":
            continue
        category = (
            "Denoising"
            if kind.startswith("denoising_") or kind == "noisy_combined"
            else (
                "Lighting"
                if kind.startswith(lighting) or kind in ("emission", "background", "ao", "shadow_catcher")
                else "Data"
            )
        )
        result.extend((kind, category + " / " + entry["label"]))
    for kind, title in (
        ("crypto_object", "Cryptomatte / Object"),
        ("crypto_material", "Cryptomatte / Material"),
        ("crypto_asset", "Cryptomatte / Asset"),
        ("aov_color", "Custom / Color AOV"),
        ("aov_value", "Custom / Value AOV"),
        ("lightgroup", "Lighting / Light Group"),
    ):
        result.extend((kind, title))
    return result


def add_from_menu(kwargs):
    kind = kwargs.get("script_value", "")
    if kind in menu()[::2]:
        add(kwargs["node"], kind)


def aov_configuration(rop, at_time):
    unique = {}
    for entry in entries(rop, at_time):
        if entry["type"] not in ("aov_color", "aov_value"):
            continue
        name = entry["source"]
        kind = entry["type"][4:]
        if name in unique and unique[name] != kind:
            raise ValueError("Conflicting shader AOV types: " + name)
        unique[name] = kind
    result = [dict(name=name, type=kind) for name, kind in unique.items()]
    _native.validate_aovs(result)
    return result


def custom_configuration(rop, at_time):
    aovs = aov_configuration(rop, at_time)
    rows = entries(rop, at_time)
    groups = list(dict.fromkeys(entry["source"] for entry in rows if entry["type"] == "lightgroup"))
    _native.validate_pass_outputs(aovs, groups)
    settings = {
        name: any(entry["type"] == "crypto_" + name for entry in rows) for name in ("object", "material", "asset")
    }
    settings["levels"] = max([entry["levels"] for entry in rows if entry["type"].startswith("crypto_")] + [2])
    return dict(aovs=aovs, lightgroups=groups, cryptomatte=settings)


def catalogue():
    return [dict(entry, label=label(entry["id"]), managed=entry["id"] in MANAGED) for entry in _native.pass_catalogue()]


def configuration(rop, at_time):
    return list(
        dict.fromkeys(["combined"] + [entry["type"] for entry in entries(rop, at_time) if entry["type"] not in SPECIAL])
    )
