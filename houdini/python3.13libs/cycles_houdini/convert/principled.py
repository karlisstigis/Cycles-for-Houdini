# SPDX-License-Identifier: Apache-2.0
"""Houdini Principled Shader (principledshader::2.0) -> Cycles Principled BSDF plan.

Pure Python: the input is a snapshot of the shader's parameter values (see
``host.snapshot_principled``). Houdini multiplies every texture with its
parameter value; the same products are built here.
"""

import math

from . import spaces
from .plan import WHITE, WRAP, Plan, Socket, absorption, as_color, close, is_constant

MONO = {0: "luminance", 1: "r", 2: "g", 3: "b"}
NORMAL_SPACE = {"uvtangent": "tangent", "object": "object", "world": "world"}

# Parameters that take an optional single-channel texture: parameter -> (Cycles input, label)
MONO_INPUTS = {
    "metallic": ("metallic", "Metallic"),
    "rough": ("roughness", "Roughness"),
    "ior": ("ior", "IOR"),
    "coat": ("coat_weight", "Coat"),
    "coatrough": ("coat_roughness", "Coat Roughness"),
    "transparency": ("transmission_weight", "Transparency"),
    "sss": ("subsurface_weight", "Subsurface"),
    "sssdist": ("subsurface_scale", "Subsurface Distance"),
    "sheen": ("sheen_weight", "Sheen"),
    "aniso": ("anisotropic", "Anisotropy"),
    "anisodir": ("anisotropic_rotation", "Anisotropy Direction"),
}

LABELS = {
    "basecolor": "Base Color",
    "albedomult": "Albedo Multiplier",
    "reflect": "Reflectivity",
    "reflecttint": "Reflect Tint",
    "transcolor": "Transmission Color",
    "transdist": "At Distance",
    "dispersion": "Dispersion",
    "ssscolor": "Subsurface Color",
    "sheentint": "Sheen Tint",
    "emitcolor": "Emission Color",
    "emitint": "Emission Intensity",
    "opac": "Opacity Scale",
    "opaccolor": "Opacity Color",
    "occlusion": "Occlusion",
}
LABELS.update({key: label for key, (_, label) in MONO_INPUTS.items()})

# Texture slots whose map has no faithful Cycles route.
UNSUPPORTED_TEXTURES = {
    "reflecttint": "a textured Reflect Tint has no Cycles input; the constant value is used",
    "sheentint": "a textured Sheen Tint has no Cycles input; the constant value is used",
    "transcolor": "a textured Transmission Color cannot drive Cycles absorption; the constant value is used",
    "transdist": "a textured At Distance cannot drive Cycles absorption; the constant value is used",
    "dispersion": "Dispersion is not carried",
    "ssscolor": "Cycles scatters subsurface light with Base Color; the map is not used",
    "occlusion": "Cycles computes occlusion itself; occlusion maps are not used",
}


class _Principled:
    def __init__(self, source):
        self.p = dict(source.get("parms", {}))
        self.plan = Plan(source=source.get("name", ""))
        self.source = source
        self._uv = False
        self._uv_value = None
        self.read = set()  # parameters the mapping used

    def get(self, name, default=None):
        self.read.add(name)
        return self.p.get(name, default)

    def on(self, name):
        return bool(self.get(name, 0))

    # -- textures ------------------------------------------------------------------
    def uv(self):
        """Principled UV Transform.

        Houdini looks textures up at uv * inverse(maketransform(SRT, T, R, S)),
        i.e. ((uv - T) rotated back) / S: the Cycles Mapping node's Texture type.
        Rotation is in degrees in Houdini, radians in Cycles; the sign was
        matched against a Karma render (rotate 30).
        """
        if self._uv:
            return self._uv_value
        self._uv = True
        translate = tuple(self.get("uvtrans", (0.0, 0.0)))
        rotate = float(self.get("uvrot", 0.0))
        scale = tuple(self.get("uvscale", (1.0, 1.0)))
        if close(translate, (0.0, 0.0, 0.0)) and abs(rotate) < 1e-9 and close(scale, (1.0, 1.0, 1.0)):
            return None
        plan = self.plan
        coordinates = plan.add("texture_coordinate", "texture_coordinates")
        mapping = plan.add(
            "mapping",
            "uv_transform",
            mapping_type="texture",
            location=(translate[0], translate[1], 0.0),
            rotation=(0.0, 0.0, -math.radians(rotate)),
            scale=(scale[0], scale[1], 1.0),
        )
        plan.set(mapping, "vector", Socket(coordinates, "UV", "vector"))
        self._uv_value = Socket(mapping, "vector", "vector")
        return self._uv_value

    def texture(self, prefix, file_parm=None, space_parm=None, wrap_parm=None, data=False, name=None):
        """The Image Texture node key for a texture slot, or None."""
        filename = self.get(file_parm or prefix + "_texture", "")
        if not filename:
            return None
        space = spaces.houdini(
            self.get(space_parm or prefix + "_textureColorSpace", "auto"), data, self.plan, LABELS.get(prefix, prefix)
        )
        wrap = WRAP.get(self.get(wrap_parm or prefix + "_textureWrap", "repeat"), "periodic")
        return self.plan.image(filename, space, wrap, "linear", self.uv(), name or prefix + "_texture")

    def mono(self, prefix):
        """parameter * texture channel for a single-value slot."""
        value = float(self.get(prefix, 0.0))
        if not self.on(prefix + "_useTexture"):
            return value
        key = self.texture(prefix, data=True)
        if key is None:
            self.plan.skip(LABELS[prefix], "Use Texture is on but no file is set")
            return value
        channel = MONO.get(int(self.get(prefix + "_monoChannel", 0)), "luminance")
        sampled = self.plan.channel(Socket(key, "color"), channel, prefix + "_channel")
        return self.plan.multiply(value, sampled, prefix + "_scale")

    def tinted(self, prefix, constant):
        """Colour slot: texture * lerp(white, colour, Tint Intensity)."""
        if not self.on(prefix + "_useTexture"):
            return constant, None
        key = self.texture(prefix, data=False)
        if key is None:
            self.plan.skip(LABELS[prefix], "Use Texture is on but no file is set")
            return constant, None
        intensity = float(self.get(prefix + "_textureIntensity", 1.0))
        tint = self.plan.lerp(WHITE, constant, intensity)
        return self.plan.multiply(Socket(key, "color"), tint, prefix + "_tint"), key

    # -- the shader ----------------------------------------------------------------
    def build(self):
        plan, surface = self.plan, "surface"
        self.reject_unsupported()

        base_constant = as_color(self.get("basecolor", (0.2, 0.2, 0.2)))
        base, base_texture = self.tinted("basecolor", base_constant)
        if self.on("basecolor_usePointColor"):
            cd = plan.attribute("Cd", "point_color")
            base = plan.multiply(base, Socket(cd, "color"), "point_color_tint")
        base = plan.multiply(base, float(self.get("albedomult", 1.0)), "albedo_multiplier")
        plan.set(surface, "base_color", base)

        for prefix in ("metallic", "rough", "ior", "coat", "coatrough", "transparency", "sss", "sheen"):
            plan.set(surface, MONO_INPUTS[prefix][0], self.mono(prefix))

        self.reflection(base)
        self.anisotropy()
        self.transmission()
        self.subsurface()
        self.sheen(base)
        self.emission()
        self.opacity(base_texture)
        self.normals()
        self.displacement()
        self.animated()
        return plan

    def reflection(self, base):
        plan, surface = self.plan, "surface"
        reflect = self.mono("reflect")
        if is_constant(reflect) and reflect > 2.0:
            plan.approx("Reflectivity", "values above 2 exceed Cycles IOR Level; clamped to 1")
            reflect = 2.0
        plan.set(surface, "specular_ior_level", plan.multiply(reflect, 0.5, "reflectivity_level"))
        tint = float(self.get("reflecttint", 0.0))
        if tint:
            plan.set(surface, "specular_tint", plan.lerp(WHITE, base, tint, "reflect_tint"))
            if not (is_constant(self.get("metallic", 0.0)) and float(self.get("metallic", 0.0)) == 0.0):
                plan.approx("Reflect Tint", "on metals Cycles also tints the edge reflection")

    def anisotropy(self):
        plan, surface = self.plan, "surface"
        aniso = self.mono("aniso")
        if is_constant(aniso) and aniso == 0.0:
            return
        plan.set(surface, "anisotropic", aniso)
        if is_constant(aniso) and aniso < 0.0:
            # Negative Houdini anisotropy stretches across the direction: rotate a quarter turn.
            plan.set(surface, "anisotropic", -aniso)
            plan.set(surface, "anisotropic_rotation", plan.add_values(self.mono("anisodir"), 0.25))
        else:
            plan.set(surface, "anisotropic_rotation", self.mono("anisodir"))
        tangent = plan.add("tangent", "uv_tangent", direction_type="uv_map")
        plan.set(surface, "tangent", Socket(tangent, "tangent", "vector"))

    def transmission(self):
        plan = self.plan
        weight = plan.value("surface", "transmission_weight", 0.0)
        if self.on("transcolor_usePointColor"):
            plan.skip("Transmission Color", "Use Point Color is not carried")
        if is_constant(weight) and weight == 0.0:
            return
        color = as_color(self.get("transcolor", WHITE))
        if close(color, WHITE):
            return
        volume = absorption(color, float(self.get("transdist", 0.1)))
        if volume is None:
            return
        key = plan.add("absorption_volume", "transmission_absorption", color=volume[0], density=volume[1])
        plan.set("output", "volume", Socket(key, "volume", "closure"))

    def subsurface(self):
        plan, surface = self.plan, "surface"
        weight = plan.value(surface, "subsurface_weight", 0.0)
        if self.on("ssscolor_usePointColor"):
            plan.skip("Subsurface Color", "Use Point Color is not carried")
        if is_constant(weight) and weight == 0.0:
            return
        plan.set(surface, "subsurface_scale", self.mono("sssdist"))
        plan.set(surface, "subsurface_radius", WHITE)
        model = self.get("sssmodel", "pbrsss")
        plan.set(surface, "subsurface_method", "random_walk")
        if model == "pbrsingles":
            plan.approx("SSS Mode", "single scattering is not available in Cycles; Random Walk is used")
        phase = float(self.get("sssphase", 0.0))
        plan.set(surface, "subsurface_anisotropy", phase)
        if not close(as_color(self.get("ssscolor", WHITE)), WHITE):
            plan.approx("Subsurface Color", "Cycles scatters subsurface light with Base Color")

    def sheen(self, base):
        plan, surface = self.plan, "surface"
        weight = plan.value(surface, "sheen_weight", 0.0)
        if is_constant(weight) and weight == 0.0:
            return
        tint = float(self.get("sheentint", 0.0))
        plan.set(surface, "sheen_tint", plan.lerp(WHITE, base, tint, "sheen_tint"))
        plan.approx("Sheen", "Cycles uses a microfibre sheen model; the falloff differs")

    def emission(self):
        plan, surface = self.plan, "surface"
        strength = float(self.get("emitint", 0.0))
        plan.set(surface, "emission_strength", strength)
        if not strength:
            return
        color, _ = self.tinted("emitcolor", as_color(self.get("emitcolor", WHITE)))
        if self.on("emitcolor_usePointColor"):
            cd = plan.attribute("Cd", "point_color")
            color = plan.multiply(color, Socket(cd, "color"), "emission_point_color")
        plan.set(surface, "emission_color", color)
        if not self.on("emitillum"):
            plan.skip("Emission Illuminates Objects", "Cycles emission always lights the scene")

    def opacity(self, base_texture):
        plan, surface = self.plan, "surface"
        mode = self.get("alphablendmode", "blend")
        if mode == "opaque":
            plan.set(surface, "alpha", 1.0)
            return
        color = as_color(self.get("opaccolor", WHITE))
        if not close(color, (color[0],) * 3):
            plan.approx("Opacity Color", "Cycles opacity is one value; the colour's average is used")
        alpha = plan.multiply(float(self.get("opac", 1.0)), sum(color) / 3.0)
        if self.on("opaccolor_useTexture"):
            key = self.texture("opaccolor", data=False)
            if key is None:
                plan.skip("Opacity Color", "Use Texture is on but no file is set")
            else:
                intensity = float(self.get("opaccolor_textureIntensity", 1.0))
                tint = plan.lerp(1.0, sum(color) / 3.0, intensity)
                alpha = plan.multiply(float(self.get("opac", 1.0)), tint)
                alpha = plan.multiply(alpha, plan.to_float(Socket(key, "color"), "opacity_luminance"), "opacity")
        if self.on("basecolor_useTextureAlpha") and base_texture is not None:
            alpha = plan.multiply(alpha, Socket(base_texture, "alpha", "float"), "texture_alpha")
        if mode == "mask" and not is_constant(alpha):
            key = plan.add("math", "alpha_cutoff", math_type="greater_than")
            plan.set(key, "value1", alpha)
            plan.set(key, "value2", float(self.get("alphacutoff", 0.5)))
            alpha = Socket(key, "value", "float")
        elif mode == "mask":
            alpha = 1.0 if alpha >= float(self.get("alphacutoff", 0.5)) else 0.0
        plan.set(surface, "alpha", alpha)
        if self.on("opacpackedalpha"):
            plan.skip("Use Packed Alpha", "packed-primitive alpha is not carried")

    def normal_chain(self, prefix, label):
        """Base or coat bump/normal: returns a normal Socket or None."""
        plan = self.plan
        kind = self.get(prefix + "BumpAndNormal_type", "normal")
        if kind == "normal":
            p = prefix + "Normal_"
            key = self.texture(
                prefix + "Normal",
                file_parm=p + "texture",
                space_parm=p + "colorspace",
                wrap_parm=p + "wrap",
                data=True,
                name=prefix + "_normal_texture",
            )
            if key is None:
                plan.skip(label, "the normal map has no file")
                return None
            if self.get(p + "imagePlane", ""):
                plan.skip(label, "image planes are not carried; the main image is used")
            color = Socket(key, "color")
            flip_x, flip_y = self.on(p + "flipX"), self.on(p + "flipY")
            signed = int(self.get(p + "space", 0)) == 1
            if signed or flip_x or flip_y:
                # Into Cycles' 0..1 encoding: c*s + o per channel.
                scale = [0.5 if signed else 1.0] * 3
                offset = [0.5 if signed else 0.0] * 3
                for axis, flip in ((0, flip_x), (1, flip_y)):
                    if flip:
                        scale[axis], offset[axis] = -scale[axis], 1.0 - offset[axis]
                encode = plan.add(
                    "vector_math", prefix + "_normal_encoding", math_type="multiply_add", vector2=tuple(scale)
                )
                plan.set(encode, "vector1", color)
                plan.set(encode, "vector3", tuple(offset))
                color = Socket(encode, "vector", "color")
            space = NORMAL_SPACE.get(self.get(p + "vectorSpace", "uvtangent"), "tangent")
            # Base explicitly: the node's own default is Blender's (Displaced).
            normal = plan.add(
                "normal_map",
                prefix + "_normal_map",
                space=space,
                strength=float(self.get(p + "scale", 1.0)),
                base="original",
            )
            plan.set(normal, "color", color)
            return Socket(normal, "normal", "vector")
        p = prefix + "Bump_"
        key = self.texture(
            prefix + "Bump",
            file_parm=p + "bumpTexture",
            space_parm=p + "colorSpace",
            wrap_parm=p + "wrap",
            data=True,
            name=prefix + "_bump_texture",
        )
        if key is None:
            plan.skip(label, "the bump map has no file")
            return None
        if self.get(p + "imagePlane", ""):
            plan.skip(label, "image planes are not carried; the main image is used")
        height = plan.channel(
            Socket(key, "color"), MONO.get(int(self.get(p + "channel", 0)), "luminance"), prefix + "_bump_channel"
        )
        bump = plan.add("bump", prefix + "_bump", strength=1.0, distance=float(self.get(p + "bumpScale", 0.05)))
        plan.set(bump, "height", height)
        return Socket(bump, "normal", "vector")

    def normals(self):
        plan, surface = self.plan, "surface"
        normal = None
        if self.on("baseBumpAndNormal_enable"):
            normal = self.normal_chain("base", "Bump & Normals")
        if self.on("roundedEdge_enable"):
            bevel = plan.add("bevel", "rounded_edges", radius=float(self.get("roundedEdge_radius", 0.01)))
            plan.set(bevel, "normal", normal)
            normal = Socket(bevel, "normal", "vector")
            if int(self.get("roundedEdge_mode", 0)) != 0:
                plan.approx("Round Edge Mode", "Cycles Bevel rounds concave and convex edges together")
        plan.set(surface, "normal", normal)
        coat = plan.value(surface, "coat_weight", 0.0)
        if is_constant(coat) and coat == 0.0:
            return
        if self.on("separateCoatNormals"):
            if self.on("coatBumpAndNormal_enable"):
                plan.set(surface, "coat_normal", self.normal_chain("coat", "Coat Bump & Normals"))
        elif normal is not None:
            plan.set(surface, "coat_normal", normal)

    def displacement(self):
        plan = self.plan
        if self.on("dispInput_enable"):
            plan.skip("Input Displacement", "displacement wired into the shader is not carried")
        if self.on("dispNoise_enable"):
            plan.skip("Noise Displacement", "Houdini noise has no matching Cycles texture")
        if not self.on("dispTex_enable"):
            return
        if self.on("shop_disable_displace_shader"):
            plan.skip("Texture Displacement", "Disable Displace Shader is on, so Houdini does not displace either")
            return
        key = self.texture(
            "dispTex",
            file_parm="dispTex_texture",
            space_parm="dispTex_colorSpace",
            wrap_parm="dispTex_wrap",
            data=True,
            name="displacement_texture",
        )
        if key is None:
            plan.skip("Texture Displacement", "the displacement map has no file")
            return
        offset, scale = float(self.get("dispTex_offset", -0.5)), float(self.get("dispTex_scale", 0.05))
        if self.get("dispTex_type", "disp") == "vectordisp":
            space = {"uvtangent": "tangent", "object": "object", "world": "world"}.get(
                self.get("dispTex_vectorSpace", "uvtangent"), "tangent"
            )
            node = plan.add("vector_displacement", "vector_displacement", space=space, midlevel=-offset, scale=scale)
            plan.set(node, "vector", Socket(key, "color", "vector"))
            if self.get("dispTex_channelOrder", "xyz") != "xyz":
                plan.skip("Channel Order", "only XYZ order is carried")
        else:
            height = plan.channel(
                Socket(key, "color"), MONO.get(int(self.get("dispTex_channel", 0)), "luminance"), "displacement_channel"
            )
            node = plan.add("displacement", "displacement", space="object", midlevel=-offset, scale=scale)
            plan.set(node, "height", height)
        plan.set("output", "displacement", Socket(node, "displacement", "vector"))
        if self.on("vm_truedisplace"):
            plan.builder["cycles_displacement_method"] = "both" if self.on("vm_bumpraydisplace") else "true"
            plan.approx(
                "True Displacements",
                "Cycles moves existing vertices only; turn on Cycles subdivision on the object for fine detail",
            )
        else:
            plan.builder["cycles_displacement_method"] = "bump"

    def reject_unsupported(self):
        plan = self.plan
        for prefix, reason in UNSUPPORTED_TEXTURES.items():
            if self.on(prefix + "_useTexture") and self.get(prefix + "_texture", ""):
                plan.skip(LABELS[prefix] + " texture", reason)
        if float(self.get("dispersion", 0.0)):
            plan.skip("Dispersion", "Houdini's dispersion amount has no Cycles equivalent (Cycles uses an Abbe number)")
        if int(self.get("priority", 0)):
            plan.skip(
                "Surface Priority",
                "Cycles sets nested-dielectric priority per object (Dielectric Priority on the object's Cycles tab)",
            )
        if self.on("basecolor_usePackedColor"):
            plan.skip("Use Packed Color", "packed-primitive colour is not carried")
        if self.on("fakecausticsenabled"):
            plan.skip("Fake Caustics", "Cycles has no fake caustics; light passes through glass physically")
        if not self.on("frontface") and "frontface" in self.p:
            plan.skip("Shade Both Sides As Front", "Cycles always shades back faces")
        for name in self.source.get("connected", ()):
            plan.skip("Input '" + name + "'", "inputs wired into the Principled Shader are not carried")

    def animated(self):
        frame = self.source.get("frame")
        for name in self.source.get("animated", ()):
            if name not in self.read:
                continue
            where = " at frame " + _frame(frame) if frame is not None else ""
            self.plan.approx(
                LABELS.get(name, name), "animated or driven by an expression; the value" + where + " was copied"
            )


def _frame(frame):
    return str(int(frame)) if float(frame).is_integer() else str(frame)


def plan_principled(source):
    """Plan for a principledshader::2.0 snapshot: {'name', 'parms', 'connected', 'animated', 'frame'}."""
    return _Principled(source).build()
