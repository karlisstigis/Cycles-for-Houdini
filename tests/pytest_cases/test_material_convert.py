# SPDX-License-Identifier: Apache-2.0
"""Convert to Cycles Material: Houdini Principled, MaterialX Standard Surface and OpenPBR mappings."""

import math
import xml.etree.ElementTree as ET

import pytest
from cycles_houdini.convert import materialx, plan as plans, principled, spaces
from cycles_houdini.convert.plan import WHITE
from support.paths import ROOT

pytestmark = [pytest.mark.pure, pytest.mark.cpu]

SRGB = spaces.SRGB


def surface(plan):
    return plan.nodes["surface"].parms


def link(plan, key, input):
    for target, name, socket in plan.links:
        if (target, name) == (key, input):
            return socket
    return None


def node_of(plan, socket):
    return plan.nodes[socket.node]


def approx(a, b):
    if isinstance(a, tuple):
        return all(math.isclose(x, y, rel_tol=1e-6, abs_tol=1e-7) for x, y in zip(a, b)) and len(a) == len(b)
    return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-7)


def labels(entries):
    return [label for label, _ in entries]


# ------------------------------------------------------------------ Principled Shader
BASE = dict(basecolor_usePointColor=0, frontface=1, emitillum=1)


def ps(**parms):
    return principled.plan_principled(dict(name="/mat/ps", parms={**BASE, **parms}))


def test_principled_constants_land_on_the_principled_bsdf():
    plan = ps(
        basecolor=(0.5, 0.25, 0.125),
        albedomult=0.8,
        metallic=0.7,
        rough=0.35,
        ior=1.6,
        reflect=0.6,
        coat=0.4,
        coatrough=0.15,
        transparency=0.3,
        sss=0.2,
        sssdist=0.03,
        sssphase=0.25,
        sheen=0.5,
        emitint=2.5,
        emitcolor=(1.0, 0.5, 0.25),
        opac=0.75,
        aniso=0.3,
        anisodir=0.1,
    )
    s = surface(plan)
    assert approx(s["base_color"], (0.4, 0.2, 0.1))  # base colour * albedo multiplier
    assert approx(s["metallic"], 0.7) and approx(s["roughness"], 0.35) and approx(s["ior"], 1.6)
    assert approx(s["specular_ior_level"], 0.3)  # reflectivity 1 == IOR Level 0.5
    assert approx(s["coat_weight"], 0.4) and approx(s["coat_roughness"], 0.15)
    assert approx(s["transmission_weight"], 0.3)
    assert approx(s["subsurface_weight"], 0.2) and approx(s["subsurface_scale"], 0.03)
    assert s["subsurface_radius"] == WHITE and s["subsurface_method"] == "random_walk"
    assert approx(s["subsurface_anisotropy"], 0.25)
    assert approx(s["sheen_weight"], 0.5) and s["sheen_tint"] == WHITE
    assert approx(s["emission_strength"], 2.5) and approx(s["emission_color"], (1.0, 0.5, 0.25))
    assert approx(s["alpha"], 0.75)
    assert approx(s["anisotropic"], 0.3) and approx(s["anisotropic_rotation"], 0.1)
    tangent = link(plan, "surface", "tangent")
    assert node_of(plan, tangent).type == "tangent" and node_of(plan, tangent).parms["direction_type"] == "uv_map"
    assert labels(plan.approximated) == ["Sheen"]  # the sheen lobe differs, and says so
    assert plan.not_carried == []
    # Only the planned helper nodes exist: no texture nodes for constant values.
    assert {n.type for n in plan.nodes.values()} == {"principled_bsdf", "surface_output", "tangent"}


def test_point_colour_multiplies_base_colour_and_missing_cd_reads_white():
    plan = principled.plan_principled(dict(name="p", parms=dict(basecolor=(0.5, 0.5, 0.5), basecolor_usePointColor=1)))
    base = link(plan, "surface", "base_color")
    mix = node_of(plan, base)
    assert mix.type == "mix_color" and mix.parms["blend_type"] == "multiply" and mix.parms["a"] == (0.5, 0.5, 0.5)
    cd = node_of(plan, link(plan, base.node, "b"))
    assert cd.type == "attribute" and cd.parms["attribute"] == "Cd" and cd.parms["missing"] == WHITE


def test_reflect_tint_and_sheen_tint_blend_towards_base_colour():
    plan = ps(basecolor=(0.2, 0.4, 0.6), reflecttint=0.5, sheen=1.0, sheentint=1.0)
    s = surface(plan)
    assert approx(s["specular_tint"], (0.6, 0.7, 0.8))
    assert approx(s["sheen_tint"], (0.2, 0.4, 0.6))


def test_negative_anisotropy_turns_a_quarter():
    s = surface(ps(aniso=-0.4, anisodir=0.1))
    assert approx(s["anisotropic"], 0.4) and approx(s["anisotropic_rotation"], 0.35)


def test_transmission_colour_at_distance_becomes_volume_absorption():
    plan = ps(transparency=1.0, transcolor=(0.5, 0.8, 1.0), transdist=0.2)
    volume = link(plan, "output", "volume")
    node = node_of(plan, volume)
    assert node.type == "absorption_volume"
    color, density = node.parms["color"], node.parms["density"]
    for channel, target in zip(color, (0.5, 0.8, 1.0)):
        sigma = density * (1.0 - channel)  # Cycles absorption coefficient
        assert approx(math.exp(-sigma * 0.2), target)
    # White transmission colour: clear glass, no volume.
    assert link(ps(transparency=1.0, transcolor=WHITE), "output", "volume") is None


def test_principled_textures_keep_paths_spaces_and_multiply_their_parameter():
    plan = ps(
        basecolor=(0.5, 1.0, 1.0),
        basecolor_useTexture=1,
        basecolor_texture="$HIP/tex/wood.<UDIM>.png",
        basecolor_textureColorSpace="sRGB",
        basecolor_textureIntensity=1.0,
        rough=0.8,
        rough_useTexture=1,
        rough_texture="$JOB/tex/wood_rough.%(UDIM)d.exr",
        rough_textureColorSpace="linear",
        rough_monoChannel=2,
        rough_textureWrap="streak",
        metallic=1.0,
        metallic_useTexture=1,
        metallic_texture="metal.png",
        metallic_monoChannel=0,
    )
    base = node_of(plan, link(plan, "surface", "base_color"))
    assert base.type == "mix_color" and base.parms["blend_type"] == "multiply" and base.parms["b"] == (0.5, 1.0, 1.0)
    image = node_of(plan, link(plan, base.name, "a"))
    assert image.type == "image_texture"
    assert image.parms["filename"] == "$HIP/tex/wood.<UDIM>.png"
    assert image.parms["colorspace"].candidates == SRGB
    rough = node_of(plan, link(plan, "surface", "roughness"))
    assert rough.type == "math" and rough.parms["math_type"] == "multiply" and approx(rough.parms["value1"], 0.8)
    channel = link(plan, rough.name, "value2")
    assert channel.output == "g" and node_of(plan, channel).type == "separate_color"
    texture = node_of(plan, link(plan, channel.node, "color"))
    assert texture.parms["filename"] == "$JOB/tex/wood_rough.<UDIM>.exr"  # Mantra token -> <UDIM>
    assert texture.parms["colorspace"].candidates == ("data",) and texture.parms["extension"] == "clamp"
    metal = link(plan, "surface", "metallic")  # parameter 1.0: the luminance itself
    assert node_of(plan, metal).type == "rgb_to_bw"


def test_tint_intensity_blends_base_colour_into_the_texture_tint():
    plan = ps(
        basecolor=(0.2, 0.4, 1.0),
        basecolor_useTexture=1,
        basecolor_texture="a.png",
        basecolor_textureIntensity=0.5,
    )
    mix = node_of(plan, link(plan, "surface", "base_color"))
    assert approx(mix.parms["b"], (0.6, 0.7, 1.0))


def test_uv_transform_drives_a_shared_mapping():
    plan = ps(
        uvtrans=(0.25, 0.5),
        uvrot=90.0,
        uvscale=(2.0, 3.0),
        basecolor_useTexture=1,
        basecolor_texture="a.png",
        rough_useTexture=1,
        rough_texture="r.png",
    )
    mappings = [n for n in plan.nodes.values() if n.type == "mapping"]
    assert len(mappings) == 1
    m = mappings[0].parms
    assert m["location"] == (0.25, 0.5, 0.0) and m["scale"] == (2.0, 3.0, 1.0)
    assert m["mapping_type"] == "texture"  # Houdini divides by Scale: (uv - T) rotated back / S
    assert approx(m["rotation"], (0.0, 0.0, -math.pi / 2))
    images = [key for key, n in plan.nodes.items() if n.type == "image_texture"]
    assert all(link(plan, key, "vector").node == mappings[0].name for key in images)


def test_normal_map_spaces_flips_and_coat_sharing():
    plan = ps(
        coat=1.0,
        baseBumpAndNormal_enable=1,
        baseBumpAndNormal_type="normal",
        baseNormal_texture="n.<UDIM>.png",
        baseNormal_colorspace="linear",
        baseNormal_vectorSpace="uvtangent",
        baseNormal_scale=0.5,
        baseNormal_flipY=1,
    )
    normal = link(plan, "surface", "normal")
    node = node_of(plan, normal)
    assert node.type == "normal_map" and node.parms["space"] == "tangent" and node.parms["strength"] == 0.5
    encode = node_of(plan, link(plan, node.name, "color"))
    assert encode.type == "vector_math" and encode.parms["vector2"] == (1.0, -1.0, 1.0)
    assert encode.parms["vector3"] == (0.0, 1.0, 0.0)
    image = node_of(plan, link(plan, encode.name, "vector1"))
    assert image.parms["colorspace"].candidates == ("data",)
    assert link(plan, "surface", "coat_normal") == normal  # no separate coat normals: the coat shares them


def test_bump_map_uses_effect_scale_as_distance():
    plan = ps(
        baseBumpAndNormal_enable=1,
        baseBumpAndNormal_type="bump",
        baseBump_bumpTexture="h.exr",
        baseBump_bumpScale=0.02,
        baseBump_channel=1,
    )
    bump = node_of(plan, link(plan, "surface", "normal"))
    assert bump.type == "bump" and bump.parms["distance"] == 0.02 and bump.parms["strength"] == 1.0
    assert link(plan, bump.name, "height").output == "r"


def test_texture_displacement_offset_and_scale():
    plan = ps(
        shop_disable_displace_shader=0,
        dispTex_enable=1,
        dispTex_texture="d.exr",
        dispTex_offset=-0.5,
        dispTex_scale=0.1,
        vm_truedisplace=1,
        vm_bumpraydisplace=1,
    )
    node = node_of(plan, link(plan, "output", "displacement"))
    assert node.type == "displacement" and node.parms["midlevel"] == 0.5 and node.parms["scale"] == 0.1
    assert plan.builder["cycles_displacement_method"] == "both"
    assert "True Displacements" in labels(plan.approximated)
    disabled = ps(shop_disable_displace_shader=1, dispTex_enable=1, dispTex_texture="d.exr")
    assert link(disabled, "output", "displacement") is None
    assert "Texture Displacement" in labels(disabled.not_carried)


def test_alpha_modes():
    assert surface(ps(alphablendmode="opaque", opac=0.2))["alpha"] == 1.0
    assert surface(ps(alphablendmode="mask", opac=0.4, alphacutoff=0.5))["alpha"] == 0.0
    plan = ps(alphablendmode="mask", basecolor_useTexture=1, basecolor_texture="a.png", basecolor_useTextureAlpha=1)
    cutoff = node_of(plan, link(plan, "surface", "alpha"))
    assert cutoff.type == "math" and cutoff.parms["math_type"] == "greater_than"
    assert link(plan, cutoff.name, "value1").output == "alpha"


def test_principled_reports_what_is_not_carried():
    plan = principled.plan_principled(
        dict(
            name="/mat/glass",
            parms=dict(
                frontface=1,
                emitillum=1,
                dispersion=0.3,
                priority=2,
                fakecausticsenabled=1,
                transcolor_useTexture=1,
                transcolor_texture="t.png",
                occlusion_useTexture=1,
                occlusion_texture="ao.png",
                dispNoise_enable=1,
                rough_useTexture=1,
                rough_texture="",
            ),
            connected=["basecolor"],
            animated=["rough"],
            frame=12,
        )
    )
    missing = labels(plan.not_carried)
    for label in (
        "Dispersion",
        "Surface Priority",
        "Fake Caustics",
        "Transmission Color texture",
        "Occlusion texture",
        "Noise Displacement",
        "Roughness",
        "Input 'basecolor'",
    ):
        assert label in missing, (label, missing)
    assert ("Roughness", "animated or driven by an expression; the value at frame 12 was copied") in plan.approximated
    message = plan.message()
    assert message.startswith("Converted from /mat/glass.\nNot carried:\n- ")
    assert "Approximated:" in message
    assert principled.plan_principled(dict(name="x", parms=dict(BASE))).message() == ""


# ------------------------------------------------------------------ colour spaces
def test_colour_space_mapping_and_resolution():
    assert spaces.houdini("auto", False).candidates == ("auto",)
    assert spaces.houdini("linear", False).candidates == ("scene_linear",)
    assert spaces.houdini("linear", True).candidates == ("data",)
    assert spaces.houdini("raw", False).candidates == ("data",)
    assert spaces.houdini("ACEScg", False).candidates == ("ACEScg",)
    assert spaces.materialx("", True).candidates == ("auto",)
    assert spaces.materialx("srgb_texture", True).candidates == SRGB
    assert spaces.materialx("srgb_texture", False).candidates == ("data",)  # value images ignore colour spaces
    assert spaces.materialx("Raw", True).candidates == ("data",)
    assert spaces.materialx("lin_rec709", True).candidates[0] == "Linear Rec.709 (sRGB)"
    menu = ("auto", "data", "scene_linear", "sRGB Encoded Rec.709 (sRGB)", "ACEScg")
    assert spaces.resolve(spaces.materialx("srgb_tx", True), menu) == "sRGB Encoded Rec.709 (sRGB)"
    assert spaces.resolve(spaces.materialx("srgb_tx", True), ("auto",)) == SRGB[0]
    assert spaces.resolve(spaces.materialx("acescg", True), menu) == "ACEScg"
    plan = plans.Plan()
    assert spaces.houdini("rec709", False, plan, "Base Color").candidates == ("auto",)
    assert labels(plan.approximated) == ["Base Color"]


# ------------------------------------------------------------------ MaterialX
def graph(surface_type, surface_parms, extra=None, inputs=None, displacement=None):
    nodes = {"/mat/b/surf": dict(type=surface_type, name="surf", parms=surface_parms, inputs=inputs or {})}
    nodes.update(extra or {})
    return dict(name="/mat/b", surface="/mat/b/surf", displacement=displacement, nodes=nodes)


def test_standard_surface_constants():
    plan = materialx.plan_materialx(
        graph(
            "mtlxstandard_surface",
            dict(
                signature="default",
                base=0.5,
                base_color=(0.8, 0.4, 0.2),
                diffuse_roughness=0.3,
                metalness=0.9,
                specular=0.8,
                specular_color=(1.0, 0.9, 0.8),
                specular_roughness=0.25,
                specular_IOR=1.7,
                specular_anisotropy=0.5,
                specular_rotation=0.2,
                sheen=0.6,
                sheen_color=(0.9, 0.8, 0.7),
                sheen_roughness=0.4,
                coat=0.7,
                coat_color=(0.95, 0.9, 0.85),
                coat_roughness=0.05,
                coat_IOR=1.4,
                thin_film_thickness=400.0,
                thin_film_IOR=1.33,
                emission=3.0,
                emission_color=(1.0, 0.5, 0.0),
                opacity=(0.5, 0.5, 0.5),
                thin_walled=1,
            ),
        )
    )
    s = surface(plan)
    assert approx(s["base_color"], (0.4, 0.2, 0.1))
    assert approx(s["diffuse_roughness"], 0.3) and approx(s["metallic"], 0.9)
    assert approx(s["specular_ior_level"], 0.4) and approx(s["specular_tint"], (1.0, 0.9, 0.8))
    assert approx(s["roughness"], 0.25) and approx(s["ior"], 1.7)
    assert approx(s["anisotropic"], 0.5) and approx(s["anisotropic_rotation"], 0.2)
    assert (
        approx(s["sheen_weight"], 0.6)
        and approx(s["sheen_tint"], (0.9, 0.8, 0.7))
        and approx(s["sheen_roughness"], 0.4)
    )
    assert approx(s["coat_weight"], 0.7) and approx(s["coat_tint"], (0.95, 0.9, 0.85))
    assert approx(s["coat_roughness"], 0.05) and approx(s["coat_ior"], 1.4)
    assert approx(s["thin_film_thickness"], 400.0) and approx(s["thin_film_ior"], 1.33)
    assert approx(s["emission_strength"], 3.0) and approx(s["emission_color"], (1.0, 0.5, 0.0))
    assert approx(s["alpha"], 0.5) and s["thin_wall"] == 1
    assert plan.not_carried == [] and plan.approximated == []


def test_standard_surface_full_transmission_uses_transmission_colour_as_base():
    plan = materialx.plan_materialx(
        graph(
            "mtlxstandard_surface",
            dict(base_color=(0.8, 0.8, 0.8), transmission=1.0, transmission_color=(0.6, 0.9, 1.0)),
        )
    )
    assert approx(surface(plan)["base_color"], (0.6, 0.9, 1.0)) and surface(plan)["transmission_weight"] == 1.0
    assert plan.approximated == []
    deep = materialx.plan_materialx(
        graph(
            "mtlxstandard_surface",
            dict(transmission=1.0, transmission_color=(0.6, 0.9, 1.0), transmission_depth=0.5),
        )
    )
    assert surface(deep)["base_color"] == WHITE
    assert node_of(deep, link(deep, "output", "volume")).type == "absorption_volume"
    partial = materialx.plan_materialx(
        graph(
            "mtlxstandard_surface",
            dict(transmission=0.5, transmission_color=(0.6, 0.9, 1.0), transmission_dispersion=20.0),
        )
    )
    assert "Transmission Color" in labels(partial.approximated)
    assert "Transmission Dispersion" in labels(partial.not_carried)


def test_standard_surface_textures_normal_map_and_colour_correct():
    nodes = {
        "/mat/b/img": dict(
            type="mtlximage",
            name="albedo",
            parms=dict(
                signature="color3",
                file="$HIP/tex/albedo.<UDIM>.jpg",
                filecolorspace="srgb_texture",
                uaddressmode="clamp",
                vaddressmode="clamp",
            ),
            inputs={},
        ),
        "/mat/b/cc": dict(
            type="mtlxcolorcorrect",
            name="cc",
            parms=dict(
                signature="default",
                gamma=2.0,
                gain=1.5,
                exposure=1.0,
                contrast=1.0,
                contrastpivot=0.5,
                hue=0.0,
                saturation=1.0,
                lift=0.0,
            ),
            inputs={"in": ["/mat/b/img", "out"]},
        ),
        "/mat/b/rough": dict(
            type="mtlximage",
            name="rough",
            parms=dict(signature="float", file="rough.png", filecolorspace="srgb_texture"),
            inputs={},
        ),
        "/mat/b/nimg": dict(type="mtlximage", name="nrm", parms=dict(signature="vector3", file="n.png"), inputs={}),
        "/mat/b/nmap": dict(
            type="mtlxnormalmap",
            name="normalmap",
            parms=dict(signature="default", scale=1.0),
            inputs={"in": ["/mat/b/nimg", "out"]},
        ),
        "/mat/b/noise": dict(type="mtlxnoise2d", name="noise", parms={}, inputs={}),
    }
    plan = materialx.plan_materialx(
        graph(
            "mtlxstandard_surface",
            dict(base=1.0, specular_roughness=0.5, metalness=0.25),
            nodes,
            inputs={
                "base_color": ["/mat/b/cc", "out"],
                "specular_roughness": ["/mat/b/rough", "out"],
                "normal": ["/mat/b/nmap", "out"],
                "metalness": ["/mat/b/noise", "out"],
            },
        )
    )
    exposure = node_of(plan, link(plan, "surface", "base_color"))
    assert exposure.type == "mix_color" and approx(exposure.parms["b"], (2.0, 2.0, 2.0))
    gain = node_of(plan, link(plan, exposure.name, "a"))
    assert approx(gain.parms["b"], (1.5, 1.5, 1.5))
    gamma = node_of(plan, link(plan, gain.name, "a"))
    assert gamma.type == "gamma" and gamma.parms["gamma"] == 0.5  # MaterialX gamma is 1/gamma
    image = node_of(plan, link(plan, gamma.name, "color"))
    assert image.parms["filename"] == "$HIP/tex/albedo.<UDIM>.jpg" and image.parms["extension"] == "clamp"
    assert image.parms["colorspace"].candidates == SRGB
    rough = link(plan, "surface", "roughness")
    assert rough.output == "r"  # float image: first channel, read as data
    assert node_of(plan, link(plan, rough.node, "color")).parms["colorspace"].candidates == ("data",)
    normal = node_of(plan, link(plan, "surface", "normal"))
    assert normal.type == "normal_map" and normal.parms["space"] == "tangent"
    assert node_of(plan, link(plan, normal.name, "color")).parms["colorspace"].candidates == ("data",)
    assert surface(plan)["metallic"] == 0.25  # the noise is reported, the constant kept
    assert any(label == "Metalness" and "mtlxnoise2d" in reason for label, reason in plan.not_carried)


def test_tiled_image_becomes_a_mapping():
    nodes = {
        "/mat/b/t": dict(
            type="mtlxtiledimage",
            name="tiles",
            parms=dict(
                signature="color3",
                file="t.png",
                uvtiling=(4.0, 2.0),
                uvoffset=(0.5, 0.25),
                realworldimagesize=(1.0, 1.0),
                realworldtilesize=(1.0, 1.0),
            ),
            inputs={},
        )
    }
    plan = materialx.plan_materialx(
        graph("mtlxstandard_surface", {}, nodes, inputs={"base_color": ["/mat/b/t", "out"]})
    )
    image = link(plan, "surface", "base_color")
    mapping = node_of(plan, link(plan, image.node, "vector"))
    assert mapping.parms["scale"] == (4.0, 2.0, 1.0) and mapping.parms["location"] == (-0.5, -0.25, 0.0)


def test_math_nodes_fold_or_map_to_cycles_nodes():
    nodes = {
        "/mat/b/img": dict(type="mtlximage", name="mask", parms=dict(signature="float", file="m.png"), inputs={}),
        "/mat/b/mul": dict(
            type="mtlxmultiply",
            name="mul",
            parms=dict(signature="default", in2=0.5),
            inputs={"in1": ["/mat/b/img", "out"]},
        ),
        "/mat/b/rng": dict(
            type="mtlxremap",
            name="remap",
            parms=dict(signature="default", inlow=0.0, inhigh=1.0, outlow=0.2, outhigh=0.8),
            inputs={"in": ["/mat/b/mul", "out"]},
        ),
        "/mat/b/c": dict(
            type="mtlxconstant", name="c", parms=dict(signature="color3", value_color3=(0.1, 0.2, 0.3)), inputs={}
        ),
        "/mat/b/inv": dict(
            type="mtlxinvert",
            name="inv",
            parms=dict(signature="color3", amount_color3=(1.0, 1.0, 1.0)),
            inputs={"in": ["/mat/b/c", "out"]},
        ),
    }
    plan = materialx.plan_materialx(
        graph(
            "mtlxstandard_surface",
            {},
            nodes,
            inputs={"specular_roughness": ["/mat/b/rng", "out"], "base_color": ["/mat/b/inv", "out"]},
        )
    )
    remap = node_of(plan, link(plan, "surface", "roughness"))
    assert remap.type == "map_range" and remap.parms["to_min"] == 0.2 and remap.parms["to_max"] == 0.8
    multiply = node_of(plan, link(plan, remap.name, "value"))
    assert multiply.type == "math" and multiply.parms["math_type"] == "multiply" and multiply.parms["value2"] == 0.5
    assert approx(surface(plan)["base_color"], (0.9, 0.8, 0.7))  # constant invert folded


def test_color_remap_maps_to_vector_map_range_with_its_own_clamp_parameter():
    # Vector Map Range names its clamp toggle use_clamp; `clamp` (Map Range's name) would
    # raise "Cycles parameter is missing" when the plan is built.
    nodes = {
        "/mat/b/img": dict(type="mtlximage", name="albedo", parms=dict(signature="color3", file="a.png"), inputs={}),
        "/mat/b/rng": dict(
            type="mtlxrange",
            name="range",
            parms=dict(signature="color3", inlow=0.0, inhigh=1.0, outlow=0.2, outhigh=0.8, doclamp=1),
            inputs={"in": ["/mat/b/img", "out"]},
        ),
    }
    plan = materialx.plan_materialx(
        graph("mtlxstandard_surface", {}, nodes, inputs={"base_color": ["/mat/b/rng", "out"]})
    )
    ranges = [node for node in plan.nodes.values() if node.type == "vector_map_range"]
    assert ranges and all("clamp" not in node.parms for node in ranges)
    assert sorted(node.parms["use_clamp"] for node in ranges) == [0, 1]


def test_open_pbr_mapping():
    plan = materialx.plan_materialx(
        graph(
            "mtlxopen_pbr_surface",
            dict(
                base_weight=1.0,
                base_color=(0.5, 0.5, 0.5),
                base_metalness=0.0,
                specular_weight=1.0,
                specular_roughness=0.1,
                specular_ior=1.45,
                transmission_weight=1.0,
                transmission_color=(0.7, 0.9, 1.0),
                transmission_dispersion_scale=0.5,
                transmission_dispersion_abbe_number=40.0,
                fuzz_weight=0.3,
                fuzz_color=(1.0, 0.0, 0.0),
                fuzz_roughness=0.6,
                coat_weight=0.5,
                coat_darkening=0.5,
                thin_film_weight=1.0,
                thin_film_thickness=0.4,
                thin_film_ior=1.5,
                emission_luminance=10.0,
                geometry_opacity=0.9,
                geometry_thin_walled=0,
            ),
        )
    )
    s = surface(plan)
    assert approx(s["base_color"], (0.7, 0.9, 1.0)) and s["transmission_weight"] == 1.0
    assert s["transmission_dispersion_scale"] == 0.5 and s["transmission_dispersion_abbe_number"] == 40.0
    assert approx(s["sheen_weight"], 0.3) and s["sheen_tint"] == (1.0, 0.0, 0.0) and approx(s["sheen_roughness"], 0.6)
    assert approx(s["thin_film_thickness"], 400.0)  # micrometres -> nanometres
    assert approx(s["emission_strength"], 10.0) and approx(s["alpha"], 0.9) and approx(s["ior"], 1.45)
    assert "Coat Darkening" in labels(plan.not_carried)


def test_open_pbr_subsurface_radius_and_colour():
    plan = materialx.plan_materialx(
        graph(
            "mtlxopen_pbr_surface",
            dict(
                subsurface_weight=1.0,
                subsurface_color=(0.9, 0.5, 0.4),
                subsurface_radius=0.2,
                subsurface_radius_scale=(1.0, 0.4, 0.2),
            ),
        )
    )
    s = surface(plan)
    assert s["subsurface_weight"] == 1.0 and approx(s["base_color"], (0.9, 0.5, 0.4))
    assert approx(s["subsurface_scale"], 0.2) and approx(s["subsurface_radius"], (1.0, 0.4, 0.2))


def test_materialx_displacement():
    nodes = {
        "/mat/b/h": dict(type="mtlximage", name="height", parms=dict(signature="float", file="h.exr"), inputs={}),
        "/mat/b/d": dict(
            type="mtlxdisplacement",
            name="disp",
            parms=dict(signature="default", scale=0.2),
            inputs={"displacement": ["/mat/b/h", "out"]},
        ),
    }
    plan = materialx.plan_materialx(graph("mtlxstandard_surface", {}, nodes, displacement="/mat/b/d"))
    node = node_of(plan, link(plan, "output", "displacement"))
    assert node.type == "displacement" and node.parms["midlevel"] == 0.0 and node.parms["scale"] == 0.2


def test_image_extract_alpha_and_texcoord_sets():
    nodes = {
        "/mat/b/tc": dict(type="mtlxtexcoord", name="tc", parms=dict(index=1), inputs={}),
        "/mat/b/img": dict(
            type="mtlximage",
            name="rgba",
            parms=dict(signature="color4", file="a.png"),
            inputs={"texcoord": ["/mat/b/tc", "out"]},
        ),
        "/mat/b/a": dict(
            type="mtlxextract",
            name="alpha",
            parms=dict(signature="color4", index=3),
            inputs={"in": ["/mat/b/img", "out"]},
        ),
    }
    plan = materialx.plan_materialx(
        graph("mtlxopen_pbr_surface", {}, nodes, inputs={"geometry_opacity": ["/mat/b/a", "out"]})
    )
    alpha = link(plan, "surface", "alpha")
    assert alpha.output == "alpha"
    uv = node_of(plan, link(plan, alpha.node, "vector"))
    assert uv.type == "uvmap" and uv.parms["attribute"] == "uv2"


# ------------------------------------------------------------------ node menu
def test_node_menu_entry():
    raw = (ROOT / "houdini/OPmenu.xml").read_bytes()
    assert raw.count(b"\r\n") == raw.count(b"\n")
    sub = ET.fromstring(raw).find("menu/subMenu")
    assert ".menu_visible(kwargs)" in sub.find("context/expression").text
    [item] = [i for i in sub.findall("scriptItem") if i.get("id") == "cycles_convert_material"]
    assert item.find("label").text == "Convert to Cycles Material"
    expression = item.find("context/expression").text.strip()
    assert expression == '__import__("cycles_houdini.convert", fromlist=["convert"]).menu_visible(kwargs)'
    compile(expression, "OPmenu.xml", "eval")
    assert "convert.convert_selected(kwargs)" in item.find("scriptCode").text


def test_menu_hooks_never_raise_without_houdini():
    from cycles_houdini import convert

    assert convert.menu_visible({"node": object()}) is False
    assert convert.can_convert(object()) is False
