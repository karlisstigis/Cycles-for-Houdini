# SPDX-License-Identifier: Apache-2.0
"""Reconstruct Blender material records as editable native Cycles VOP graphs.

Unsupported connected nodes/settings fail explicitly. Color arithmetic remains
in Blender's working primaries; matrices at texture/closure boundaries preserve
its meaning when Houdini renders in a different linear working color space.
"""

from pathlib import Path, PurePosixPath, PureWindowsPath
import hashlib
import json
import re


# Attribute node types whose values are per object (docs/user/blender-import.md, Link mode).
_OBJECT_ATTRIBUTE_TYPES = ("OBJECT", "INSTANCER")


class BlendMaterialError(ValueError):
    pass


_TYPES = {
    "BsdfPrincipled": "principled_bsdf",
    "BsdfDiffuse": "diffuse_bsdf",
    "BsdfGlossy": "glossy_bsdf",
    "BsdfAnisotropic": "glossy_bsdf",
    "BsdfGlass": "glass_bsdf",
    "BsdfRefraction": "refraction_bsdf",
    "BsdfTransparent": "transparent_bsdf",
    "BsdfTranslucent": "translucent_bsdf",
    "BsdfToon": "toon_bsdf",
    "BsdfHair": "hair_bsdf",
    "BsdfHairPrincipled": "principled_hair_bsdf",
    "BsdfMetallic": "metallic_bsdf",
    "BsdfSheen": "sheen_bsdf",
    "SubsurfaceScattering": "subsurface_scattering",
    "Emission": "emission",
    "Holdout": "holdout",
    "AddShader": "add_closure",
    "MixShader": "mix_closure",
    "VolumeAbsorption": "absorption_volume",
    "VolumeScatter": "scatter_volume",
    "VolumePrincipled": "principled_volume",
    "RGB": "color",
    "Value": "value",
    "TexImage": "image_texture",
    "TexEnvironment": "environment_texture",
    "TexNoise": "noise_texture",
    "TexVoronoi": "voronoi_texture",
    "TexWave": "wave_texture",
    "TexGradient": "gradient_texture",
    "TexChecker": "checker_texture",
    "TexBrick": "brick_texture",
    "TexMagic": "magic_texture",
    "TexWhiteNoise": "white_noise_texture",
    "TexGabor": "gabor_texture",
    "TexCoord": "texture_coordinate",
    "UVMap": "uvmap",
    "Attribute": "attribute",
    "VertexColor": "vertex_color",
    "Mapping": "mapping",
    "Math": "math",
    "VectorMath": "vector_math",
    "MapRange": "map_range",
    "MixRGB": "mix_color",
    "Mix": "mix_color",
    "Gamma": "gamma",
    "Invert": "invert",
    "BrightContrast": "brightness_contrast",
    "HueSaturation": "hsv",
    "RGBToBW": "rgb_to_bw",
    "Blackbody": "blackbody",
    "Wavelength": "wavelength",
    "Fresnel": "fresnel",
    "LayerWeight": "layer_weight",
    "LightPath": "light_path",
    "LightFalloff": "light_falloff",
    "NewGeometry": "geometry",
    "ObjectInfo": "object_info",
    "ParticleInfo": "particle_info",
    "HairInfo": "hair_info",
    "PointInfo": "point_info",
    "CameraData": "camera_info",
    "NormalMap": "normal_map",
    "Tangent": "tangent",
    "Bump": "bump",
    "Normal": "normal",
    "Displacement": "displacement",
    "VectorDisplacement": "vector_displacement",
    "VectorRotate": "vector_rotate",
    "VectorTransform": "vector_transform",
    "SeparateXYZ": "separate_xyz",
    "CombineXYZ": "combine_xyz",
    "SeparateColor": "separate_color",
    "CombineColor": "combine_color",
    "Clamp": "clamp",
    "AmbientOcclusion": "ambient_occlusion",
    "Bevel": "bevel",
    "Wireframe": "wireframe",
    "ValToRGB": "color_ramp",
    "FloatCurve": "float_curve",
    "RGBCurve": "rgb_curves",
    "VectorCurve": "vector_curves",
    "VolumeCoefficients": "volume_coefficients",
}
# World-only node types (import_world_material): a surface material never accepts them.
_WORLD_TYPES = {"Background": "background_shader", "TexSky": "sky_texture"}


def node_kind(item, world=False):
    """The native engine type for a Blender node record, or None when there is none.

    Blender's Map Range is one node for floats and vectors; Cycles has two
    (shader.cpp: VectorMapRangeNode for data type FLOAT_VECTOR).
    """
    short = item["type"].removeprefix("ShaderNode")
    if short == "MapRange" and item.get("properties", {}).get("data_type") == "FLOAT_VECTOR":
        return "vector_map_range"
    kind = _TYPES.get(short)
    if kind is None and world:
        kind = _WORLD_TYPES.get(short)
    return kind


# Node settings. The exporter carries every RNA setting of a node
# (blend_material_export._properties); each one is mapped to a native parameter,
# consumed by the builder, listed here as not read by Blender's Cycles, or the import
# fails naming it. tests/houdini_blend_node_coverage_smoke.py derives every node type,
# setting and enum item from Blender itself and fails on a gap.
# Settings Blender's Cycles sync (intern/cycles/blender/shader.cpp) never reads, so they
# cannot change a render: (Blender node type, setting) -> reason.
UNREAD_SETTINGS = {
    ("ShaderNodeMixRGB", "use_alpha"): "Blender's Cycles reads only the blend type and clamp of a MixRGB node",
}
# Blender stores these in radians; the native parameters show degrees (the material
# graph converts them back, materials.material_graph).
_DEGREE_SETTINGS = frozenset(
    (("sky_texture", "sun_elevation"), ("sky_texture", "sun_size"), ("sky_texture", "sun_rotation"))
)
# Blender enum items whose native token is another word: (native type, parameter) ->
# {Blender item: native token}. Other items match a native token by name (property()).
_ENUM_TOKENS = {
    ("wave_texture", "profile"): {"SIN": "sine", "SAW": "saw", "TRI": "tri"},
    ("vector_rotate", "rotate_type"): {
        "AXIS_ANGLE": "axis",
        "X_AXIS": "x_axis",
        "Y_AXIS": "y_axis",
        "Z_AXIS": "z_axis",
        "EULER_XYZ": "euler_xyz",
    },
    # Cycles NodePrincipledHairParametrization: REFLECTANCE = Blender COLOR,
    # PIGMENT_CONCENTRATION = MELANIN, DIRECT_ABSORPTION = ABSORPTION.
    ("principled_hair_bsdf", "parametrization"): {
        "COLOR": "Direct coloring",
        "MELANIN": "Melanin concentration",
        "ABSORPTION": "Absorption coefficient",
    },
    ("math", "math_type"): {"FRACT": "fraction"},
}
# Blender Mix node data types -> the native Mix node's data_type (Cycles has no
# rotation mix in shaders).
_MIX_DATA_TYPES = {"FLOAT": "float", "VECTOR": "vector", "RGBA": "color"}
_MIX_FACTOR_MODES = {"UNIFORM": "uniform", "NON_UNIFORM": "non_uniform"}
# World mode: Blender evaluates these default inputs as the Blender-world view direction
# (Background shading: Position = Generated = Object = the direction), and the native
# defaults are the same quantities in Houdini world, so they get explicit wiring.
_WORLD_COORDINATE_LINKS = frozenset(("position", "texture_generated"))
_WORLD_DIRECTION_OUTPUTS = frozenset(("generated", "object", "normal", "reflection"))


# Axis adapters. blend_export keeps mesh data in Blender object space and maps every
# instance into Houdini world with blend_export._CONVERT: Houdini (x, y, z) =
# Blender (x, z, -y), a pure rotation (no scale, no translation), so points,
# directions and normals convert alike. Object-space values therefore need no
# adapter, but Cycles evaluates world-space shading quantities in Houdini world,
# while a Blender graph computes with Blender-world values (a "Position.z > 1" mask
# or a Noise lookup at Position). Wires between ordinary graph nodes carry
# Blender-world values; where one meets a native world-space port, a typed adapter
# (Separate XYZ, exact Math negation, Combine XYZ) converts it. A native world
# output feeding a native world input (Normal Map -> BSDF Normal) needs none.
_BLENDER_TO_HOUDINI = ((1, 0, 0), (0, 0, 1), (0, -1, 0))
_HOUDINI_TO_BLENDER = tuple(tuple(row[c] for row in _BLENDER_TO_HOUDINI) for c in range(3))  # transpose = inverse
# Native outputs that are Houdini-world vectors (normalized engine socket ids).
# Vector Transform's output joins them when it converts to World.
_WORLD_OUTPUTS = {
    "geometry": frozenset(("position", "normal", "truenormal", "tangent", "incoming")),
    "texture_coordinate": frozenset(("reflection",)),
    "object_info": frozenset(("location",)),
    "normal_map": frozenset(("normal",)),
    "bump": frozenset(("normal",)),
    "tangent": frozenset(("tangent",)),
    "bevel": frozenset(("normal",)),
    "displacement": frozenset(("displacement",)),
    "vector_displacement": frozenset(("displacement",)),
    "particle_info": frozenset(("location", "velocity", "angularvelocity")),
    "point_info": frozenset(("position",)),
    "hair_info": frozenset(("tangentnormal",)),
}
# A native input whose engine default is a world-space shading quantity (BSDF,
# Bump, Fresnel, ... Normal and Tangent inputs) takes Houdini-world vectors.
_WORLD_DEFAULT_LINKS = frozenset(("normal", "tangent", "position", "incoming"))
OBJECT_INFO_RANDOM_WARNING = (
    "Object Info Random differs from Blender: Cycles for Houdini derives each object's random value from its"
    " Houdini object and instance identity, so it still varies per object and instance but not with Blender's values"
)


def _axes_value(value, matrix, encoded=False):
    """Apply a signed axis permutation to a constant (encoded: a 0..1 colour, 2c - 1)."""
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return value
    result = []
    for row in matrix:
        column = next(c for c in range(3) if row[c])
        component = float(value[column])
        result.append(component if row[column] > 0 else (1.0 - component if encoded else -component))
    return result + list(value[3:])


def _norm(value):
    return re.sub("[^a-z0-9]", "", str(value).lower())


def _name(value):
    return re.sub("[^A-Za-z0-9_]", "_", value).strip("_") or "blender_material"


def _find_space(config, metadata):
    identity = metadata.get("color_interop_id", "")
    if not identity:
        raise BlendMaterialError(
            "Source OCIO color space has no portable interop identity: " + metadata.get("colorspace", "")
        )
    for space in config.getColorSpaces():
        if space.getInteropID() == identity:
            return space.getName()
    for token in (identity, metadata.get("colorspace", "")):
        space = config.getColorSpace(token)
        if space is not None and space.getInteropID() == identity:
            return space.getName()
    # Houdini's shipped OCIO 2.3 configurations predate ColorSpace interop IDs.
    # These exact standard space names have fixed primaries/transfer functions;
    # arbitrary/custom names never get an inferred interpretation.
    standards = {
        "lin_rec709_scene": ("Linear Rec.709 (sRGB)", "Linear Rec.709", "Linear Rec.709 (sRGB) - Scene-referred"),
        "srgb_rec709_display": ("sRGB - Texture", "sRGB Texture", "sRGB", "sRGB - Display"),
        "lin_ap1_scene": ("ACEScg",),
        "lin_ap0_scene": ("ACES2065-1",),
    }
    for token in standards.get(identity, ()):
        space = config.getColorSpace(token)
        if space is not None and not space.isData():
            return space.getName()
    raise BlendMaterialError("Houdini OCIO configuration cannot resolve source color identity: " + identity)


def _color_matrices(metadata):
    import PyOpenColorIO as ocio

    config = ocio.GetCurrentConfig()
    source = _find_space(config, metadata)

    def matrix(a, b):
        cpu = config.getProcessor(a, b).getDefaultCPUProcessor()
        origin = cpu.applyRGB([0.0, 0.0, 0.0])
        columns = [cpu.applyRGB([float(i == j) for i in range(3)]) for j in range(3)]
        result = [[columns[c][r] - origin[r] for c in range(3)] for r in range(3)]
        sample = [0.13, 0.47, 0.81]
        predicted = [sum(row[c] * sample[c] for c in range(3)) for row in result]
        if (
            max(abs(a - b) for a, b in zip(cpu.applyRGB(sample), predicted)) > 2e-5
            or max(abs(v) for v in origin) > 2e-5
        ):
            raise BlendMaterialError("Material working-space conversion must be scene-linear")
        return result

    return config, matrix(source, ocio.ROLE_SCENE_LINEAR), matrix(ocio.ROLE_SCENE_LINEAR, source)


# Network layout. The exporter records every node's flattened Blender position
# (`layout`: top-left corner in node-editor view units, about pixels at UI scale
# 1, y up like Houdini's network editor) plus `width`, the Material Output's
# position (`output_layout`) and optional `frames`. Mapping, in Houdini network
# units:
# * x and y scale by one factor (aspect kept): _LAYOUT_SCALE (a 240-unit Blender
#   node becomes 2.4 units, a ~300-unit Poly Haven column pitch 3 units), raised
#   up to _LAYOUT_MAX_SCALE when a VOP tile is wider relative to its Blender
#   node, so columns that did not touch in Blender do not touch here either.
# * VOPs list every input, so they are often taller than their Blender node:
#   nodes are visited top-down and a node overlapping an already placed one in
#   both x and y drops just below it (order and columns are kept).
# * Builder-owned helpers (colour conversions, axis adapters, group defaults,
#   vertex colour alpha) stand in columns just left of the node they finally
#   feed (an adapter after a producer belongs to its consumer), one column
#   per chain depth, stacked downward from that node's top. Every Blender-placed
#   node entirely left of such a consumer moves left by the helper columns' width.
# * Frames become network boxes (label as comment, custom colour); nested frames
#   nest boxes; each node goes in its innermost box with its helpers.
_LAYOUT_SCALE = 0.01
_LAYOUT_MAX_SCALE = 0.025
_LAYOUT_ROW = 0.2  # estimated height of one listed VOP connector
_LAYOUT_GAP = 0.25  # vertical gap between nodes dropped below each other
_LAYOUT_COLUMN_GAP = 0.5  # horizontal gap between helper columns
# A VOP's position is its header tile's lower-left corner; the listed connectors hang
# below it, and size() is the full height. Measured with NetworkEditor.itemRect in
# Houdini 22.0: the visible top is position.y + 0.3 for every Cycles VOP.
_LAYOUT_TILE_TOP = 0.3


def _extent(node):
    """(width, tile height, full height) of a VOP; connector lists may outgrow its tile."""
    size = node.size()
    width, tile = float(size[0]), float(size[1])
    rows = max(len(node.inputNames()), len(node.outputNames()))
    return width, tile, max(tile, 0.4 + _LAYOUT_ROW * rows)


def has_layout(record):
    """True when a record carries the exporter's node layout (older caches do not)."""
    nodes = record.get("nodes") or ()
    return (
        bool(nodes)
        and "layout" in record.get("output_layout", {})
        and all("layout" in n and "width" in n for n in nodes)
    )


class _Layout:
    def __init__(self, host, record, nodes, output):
        self.host, self.record, self.output = host, record, output
        self.mapped = [(nodes[item["id"]], item["layout"], item["width"], item["id"]) for item in record["nodes"]]
        layout = record["output_layout"]
        self.mapped.append((output, layout["layout"], layout.get("width", 140.0), "output"))
        self.extent = {}

    def size(self, node):
        key = node.path()
        if key not in self.extent:
            self.extent[key] = _extent(node)
        return self.extent[key]

    def helpers(self):
        """consumer path -> helper columns (lists of nodes), nearest column first."""
        paths = {node.path() for node, *_ in self.mapped}
        free = {child.path(): child for child in self.host.children() if child.path() not in paths}
        owned = {}
        for consumer, *_ in self.mapped:
            columns, frontier = [], [consumer]
            while frontier and free:
                column = []
                for node in frontier:
                    for source in node.inputs():
                        if source is not None and source.path() in free:
                            column.append(free.pop(source.path()))
                if column:
                    columns.append(column)
                frontier = column
            if columns:
                owned[consumer.path()] = columns
        if free:  # unreachable helpers: keep them visible beside the output
            owned.setdefault(self.output.path(), []).append(list(free.values()))
        return owned

    def place(self):
        import hou

        scale = _LAYOUT_SCALE
        for node, _, width, _ in self.mapped:
            if width > 0:
                scale = max(scale, min(_LAYOUT_MAX_SCALE, self.size(node)[0] / float(width)))
        owned = self.helpers()
        room = {}
        for consumer, (bx, _), _, _ in self.mapped:
            columns = owned.get(consumer.path(), ())
            if columns:
                room[consumer.path()] = (
                    bx,
                    sum(max(self.size(n)[0] for n in column) + _LAYOUT_COLUMN_GAP for column in columns),
                )

        boxes = {}  # path -> [left, top]
        for node, (bx, by), bw, _ in self.mapped:
            # Only nodes entirely left of the consumer move; its own column stays.
            shift = sum(width for x, width in room.values() if bx + bw <= x)
            boxes[node.path()] = [bx * scale - shift, by * scale]
        placed = []

        def settle(node):
            width, _, height = self.size(node)
            left, top = boxes[node.path()]
            moved = True
            while moved:
                moved = False
                for other_left, other_top, other_width, other_height in placed:
                    if (
                        left < other_left + other_width
                        and other_left < left + width
                        and top > other_top - other_height - _LAYOUT_GAP
                        and other_top > top - height - _LAYOUT_GAP
                    ):
                        top, moved = other_top - other_height - _LAYOUT_GAP, True
            boxes[node.path()] = [left, top]
            placed.append((left, top, width, height))

        for node, *_ in sorted(self.mapped, key=lambda m: (-boxes[m[0].path()][1], boxes[m[0].path()][0])):
            settle(node)
        for consumer, *_ in self.mapped:
            left, top = boxes[consumer.path()]
            for column in owned.get(consumer.path(), ()):
                left -= max(self.size(n)[0] for n in column) + _LAYOUT_COLUMN_GAP
                cursor = top
                for node in column:
                    boxes[node.path()] = [left, cursor]
                    settle(node)
                    cursor = boxes[node.path()][1] - self.size(node)[2] - _LAYOUT_GAP
        for node in [m[0] for m in self.mapped] + [n for cs in owned.values() for c in cs for n in c]:
            left, top = boxes[node.path()]
            node.setPosition(hou.Vector2(left, top - _LAYOUT_TILE_TOP))
        self.frames(owned)

    def frames(self, owned):
        import hou

        frames = [f for f in self.record.get("frames") or () if isinstance(f, dict) and f.get("id")]
        by_id = {f["id"]: f for f in frames}

        def depth(frame):
            seen = {frame["id"]}
            while frame.get("parent") in by_id and frame["parent"] not in seen:
                frame = by_id[frame["parent"]]
                seen.add(frame["id"])
            return len(seen)

        innermost = {}
        for frame in frames:
            for identity in frame.get("nodes", ()):
                if identity not in innermost or depth(frame) > depth(by_id[innermost[identity]]):
                    innermost[identity] = frame["id"]
        members = {identity: node for node, _, _, identity in self.mapped}
        made = {}
        for frame in sorted(frames, key=depth, reverse=True):
            items = []
            for identity, holder in innermost.items():
                node = members.get(identity)
                if holder == frame["id"] and node is not None:
                    items.append(node)
                    items.extend(n for column in owned.get(node.path(), ()) for n in column)
            items.extend(made[f["id"]] for f in frames if f.get("parent") == frame["id"] and f["id"] in made)
            if not items:
                continue
            box = self.host.createNetworkBox()
            box.setComment(str(frame.get("label") or frame.get("name") or ""))
            if frame.get("color"):
                box.setColor(hou.Color(tuple(float(c) for c in frame["color"][:3])))
            for item in items:
                box.addItem(item)
            box.fitAroundContents()
            made[frame["id"]] = box


class _Builder:
    def __init__(self, record, directory, parent, definitions, world=False):
        from .. import materials

        self.record, self.root, self.definitions = record, Path(directory).resolve(), definitions
        self.world = world  # a Blender World graph built as a Cycles World Material
        self.config, self.to_working, self.from_working = _color_matrices(record["color_management"])
        builder = materials.WORLD_BUILDER if world else materials.BUILDER
        self.host = parent.createNode(builder, _name(record["name"]) + "_" + record["key"][-8:])
        self.host.setUserData("cycles_blender_material_key", record["key"])
        self.host.setUserData("cycles_external_blend", "1")
        self.host.setUserData("cycles_blender_material_source", json.dumps(record, separators=(",", ":")))
        for child in self.host.children():
            child.destroy()
        if world:
            self.output = self.host.createNode(materials.WORLD_OUTPUT, "world_output")
        else:
            self.output = self.host.createNode(materials.SURFACE_OUTPUT, "surface_output")
        self.nodes, self.kinds, self.socket_maps, self.transforms = {}, {}, {}, {}
        self.items = {item["id"]: item for item in record["nodes"]}
        self.warnings = []  # translated, but not exactly Blender's result
        # node id -> {Blender setting: native parameter it was written to, or None when
        # consumed without one (data type, attribute type, settings Cycles never reads)}
        self.applied = {}
        self.used = set()
        for node in record["nodes"]:
            for socket in node["inputs"]:
                if "source" in socket:
                    self.used.add((socket["source"]["node"], socket["source"]["index"]))
        for terminal in record["outputs"].values():
            if "source" in terminal:
                self.used.add((terminal["source"]["node"], terminal["source"]["index"]))

    def fail(self, node, message):
        raise BlendMaterialError(self.record["name"] + " / " + node["name"] + ": " + message)

    def warn(self, node, message):
        self.warnings.append(
            dict(
                code="blender.material.approximation",
                severity="warning",
                source=self.record["name"],
                message=self.record["name"] + " / " + node["name"] + ": " + message,
            )
        )

    def parameter(self, host, key, value):
        parm = host.parmTuple(key)
        if parm is None:
            raise BlendMaterialError("Native parameter is unavailable: " + host.path() + "/" + key)
        if len(parm) == 1:
            host.parm(key).set(value)
        else:
            parm.set(tuple(value)[: len(parm)])

    def property(self, item, key, value):
        host, kind = self.nodes[item["id"]], self.kinds[item["id"]]
        definition = self.definitions.get(kind, {})
        socket = next((s for s in definition.get("inputs", ()) if s["id"] == key), None)
        tokens = _ENUM_TOKENS.get((kind, key))
        if socket and socket["type"] == "enum" and str(value) in (tokens or ()):
            value = tokens[str(value)]
        if socket and socket["type"] == "enum":
            aliases = {
                "smoothmin": "smoothmin",
                "smoothmax": "smoothmax",
                "chebyshev": "chebychev",
                "repeAt": "periodic",
                "extend": "clamp",
                "clip": "black",
                "repeat": "periodic",
                "premul": "associated",
                "straight": "unassociated",
                "none": "ignore",
                "mix": "mix",
            }
            token = aliases.get(str(value).lower(), str(value))
            matches = [v["token"] for v in socket.get("enum_values", ()) if _norm(v["token"]) == _norm(token)]
            if len(matches) != 1:
                self.fail(item, "Unsupported " + key + " value: " + str(value))
            value = matches[0]
        self.parameter(host, key, value)

    def color(self, endpoint, matrix, suffix):
        if max(abs(matrix[r][c] - float(r == c)) for r in range(3) for c in range(3)) < 2e-6:
            return endpoint
        key = endpoint[0].path(), endpoint[1], suffix
        if key in self.transforms:
            return self.transforms[key]
        combine = self.host.createNode("cycles::combine_xyz::1", "color_" + suffix)
        for row, channel in zip(matrix, ("x", "y", "z")):
            dot = self.host.createNode("cycles::vector_math::1", "color_" + suffix + "_" + channel)
            dot.parm("math_type").set("dot_product")
            dot.parmTuple("vector2").set(row)
            dot.setNamedInput("vector1", endpoint[0], endpoint[1])
            combine.setNamedInput(channel, dot, "value")
        result = combine, "vector"
        self.transforms[key] = result
        return result

    def axes(self, endpoint, matrix, suffix, encoded=False):
        """Exact signed axis permutation of a vector wire (see _BLENDER_TO_HOUDINI).

        encoded: the wire is a 0..1 colour encoding 2c - 1 of a direction (a world
        Normal Map's Color), so a negated axis becomes 1 - c instead of -c.
        """
        key = endpoint[0].path(), endpoint[1], suffix
        if key in self.transforms:
            return self.transforms[key]
        split = self.host.createNode("cycles::separate_xyz::1", suffix + "_split")
        split.setNamedInput("vector", endpoint[0], endpoint[1])
        combine = self.host.createNode("cycles::combine_xyz::1", suffix)
        for row, channel in zip(matrix, ("x", "y", "z")):
            columns = [c for c in range(3) if row[c]]
            if len(columns) != 1 or abs(row[columns[0]]) != 1:
                raise BlendMaterialError("Axis adapter requires a signed axis permutation")
            component = ("x", "y", "z")[columns[0]]
            if row[columns[0]] > 0:
                combine.setNamedInput(channel, split, component)
                continue
            flip = self.host.createNode("cycles::math::1", suffix + "_" + channel)
            if encoded:
                flip.parm("math_type").set("subtract")
                self.parameter(flip, "value1", 1.0)
                flip.setNamedInput("value2", split, component)
            else:
                flip.parm("math_type").set("multiply")
                flip.setNamedInput("value1", split, component)
                self.parameter(flip, "value2", -1.0)
            combine.setNamedInput(channel, flip, "value")
        result = combine, "vector"
        self.transforms[key] = result
        return result

    def output_space(self, source):
        """'world' when a connected native output is a Houdini-world vector."""
        kind, item = self.kinds[source["node"]], self.items[source["node"]]
        name = _norm(self.socket_maps[source["node"]]["outputs"].get(source["index"], ""))
        if kind == "vector_transform":
            return "world" if name == "vector" and item["properties"].get("convert_to") == "WORLD" else None
        if self.world and kind == "texture_coordinate" and name in _WORLD_DIRECTION_OUTPUTS:
            return "world"  # the background's view direction, in Houdini world
        return "world" if name in _WORLD_OUTPUTS.get(kind, ()) else None

    def input_space(self, item, name, native):
        """'world' / 'encoded_world' when a native input expects Houdini-world data."""
        kind, props, key = self.kinds[item["id"]], item["properties"], _norm(name)
        if kind == "vector_transform" and key == "vector":
            return "world" if props.get("convert_from") == "WORLD" else None
        if kind == "vector_displacement" and key == "vector":
            return "world" if props.get("space") == "WORLD" else None
        if kind == "normal_map" and key == "color":
            # World and Blender World both decode 2c - 1 (Blender World then
            # negates y and z); the same colour permutation fixes both.
            return "encoded_world" if props.get("space") in ("WORLD", "BLENDER_WORLD") else None
        if self.world and _WORLD_COORDINATE_LINKS.intersection(native.get("default_links") or ()):
            return None  # a lookup coordinate (Environment/Sky Texture Vector), in Blender axes
        if _WORLD_DEFAULT_LINKS.intersection(native.get("default_links") or ()):
            return "world"
        return None

    def direction(self):
        """World mode: the view direction in Blender axes (Geometry Position, adapted)."""
        key = "world_direction"
        if key not in self.transforms:
            node = self.host.createNode("cycles::geometry::1", "world_direction")
            self.transforms[key] = self.axes((node, "position"), _HOUDINI_TO_BLENDER, "blender_direction")
        return self.transforms[key]

    def adapt(self, endpoint, source, target):
        """Convert a wire between Houdini-world ports and Blender-world graph values."""
        if source == "world" and target != "world":
            endpoint = self.axes(endpoint, _HOUDINI_TO_BLENDER, "blender_axes")
        elif source != "world" and target == "world":
            endpoint = self.axes(endpoint, _BLENDER_TO_HOUDINI, "houdini_axes")
        if target == "encoded_world":
            endpoint = self.axes(endpoint, _BLENDER_TO_HOUDINI, "houdini_axes_encoded", encoded=True)
        return endpoint

    def endpoint(self, source):
        node, kind = self.nodes[source["node"]], self.kinds[source["node"]]
        name = self.socket_maps[source["node"]]["outputs"].get(source["index"])
        if name is None:
            raise BlendMaterialError("Unmapped connected Blender output: " + source["name"])
        endpoint = node, name
        original = next(n for n in self.record["nodes"] if n["id"] == source["node"])
        if kind == "vertex_color" and name == "alpha":
            layer = node.parm("layer_name").evalAsString()
            key = source["node"], "owned_alpha"
            if key not in self.transforms:
                alpha = self.host.createNode("cycles::attribute::1", "vertex_color_alpha")
                alpha.parm("attribute").set("__cycles_blender_alpha_" + layer)
                self.transforms[key] = alpha, "fac"
            return self.transforms[key]
        if kind == "attribute" and name == "alpha":
            attribute_type = original["properties"].get("attribute_type", "GEOMETRY")
            if attribute_type not in _OBJECT_ATTRIBUTE_TYPES:
                self.fail(
                    original, "Generic Attribute Alpha needs color-layer type binding; use a Color Attribute node"
                )
            # Object/Instancer attributes: Link renders carry the alpha in its own field.
            key = source["node"], "object_alpha"
            if key not in self.transforms:
                import hou

                from .blend_names import object_attribute_field

                alpha = self.host.createNode("cycles::attribute::1", "object_attribute_alpha")
                field = object_attribute_field(attribute_type, original["properties"].get("attribute_name", ""), True)
                alpha.parm("attribute").set(hou.text.encodeAttrib(field))
                self.transforms[key] = alpha, "fac"
            return self.transforms[key]
        if kind in ("image_texture", "environment_texture") and name == "color" and not original["image"]["is_data"]:
            endpoint = self.color(endpoint, self.from_working, "from_working")
        if kind in ("blackbody", "wavelength") and name == "color":
            endpoint = self.color(endpoint, self.from_working, "from_working")
        return endpoint

    def constant(self, socket):
        value = socket["value"]
        vector = isinstance(value, (list, tuple))
        node = self.host.createNode("cycles::color::1" if vector else "cycles::value::1", "group_default")
        self.parameter(node, "value", value[:3] if vector else value)
        return node, "color" if vector else "value"

    def image(self, item):
        image = item["image"]
        if image.get("packed") is False:
            # Exported with textures="in_place": the file stays where Blender reads it.
            # Not owned, so no ownership or sha256 check; a missing file keeps its path.
            filename = str(image["filename"]).replace("\\", "/")
            if image.get("missing"):
                self.warnings.append(
                    dict(
                        code="material.missing_texture",
                        severity="warning",
                        source=self.record["name"],
                        node=item["name"],
                        path=filename,
                        message=self.record["name"] + " / " + item["name"] + ": texture not found: " + filename,
                    )
                )
            elif not (PurePosixPath(filename).is_absolute() or PureWindowsPath(filename).is_absolute()):
                self.fail(item, "In-place texture path is not absolute: " + filename)
        else:
            for dependency in image["dependencies"]:
                path = (self.root / dependency["filename"]).resolve()
                if not path.is_relative_to(self.root) or not path.is_file():
                    self.fail(item, "Missing or escaping owned texture: " + str(path))
                if hashlib.sha256(path.read_bytes()).hexdigest() != dependency["sha256"]:
                    self.fail(item, "Texture changed since Blender extraction: " + str(path))
            filename = str(self.root / image["filename"])
        self.property(item, "filename", filename)
        space = "data" if image["is_data"] else _find_space(self.config, image)
        self.property(item, "colorspace", space)
        self.property(item, "alpha_type", image["alpha_mode"])
        if image["tiles"]:
            self.parameter(self.nodes[item["id"]], "tiles", " ".join(str(t) for t in image["tiles"]))

    def properties(self, item):
        kind, props = self.kinds[item["id"]], item["properties"]
        if "curve_mapping" in props:
            if kind not in ("float_curve", "rgb_curves", "vector_curves"):
                self.fail(item, "CurveMapping is unsupported for this shader type")
            self.curve(item)
        if props.get("object_reference"):
            self.fail(item, "Texture coordinate object references require an explicit imported-object binding")
        if props.get("from_instancer"):
            self.fail(item, "From Instancer (from_instancer) shader coordinates require a separate adapter")
        attribute_type = props.get("attribute_type", "GEOMETRY")
        if attribute_type != "GEOMETRY" and attribute_type not in _OBJECT_ATTRIBUTE_TYPES:
            self.fail(item, "Attribute type " + str(attribute_type) + " is not supported")
        applied = self.applied.setdefault(item["id"], {})
        aliases = dict(
            operation="math_type",
            vector_type="mapping_type",
            uv_map="attribute",
            layer_name="attribute",
            attribute_name="attribute",
            from_instancer="from_dupli",
            normalize="use_normalize",
            noise_dimensions="dimensions",
            voronoi_dimensions="dimensions",
            noise_type="type",
            wave_profile="profile",
            insides="inside",
            component="component",
            turbulence_depth="depth",
            gabor_type="type",
            rotation_type="rotate_type",
            falloff="method",
        )
        if kind == "normal_map":
            aliases["space"] = "space"
        if kind == "vertex_color":
            aliases["layer_name"] = "layer_name"
        if kind == "vector_transform":
            aliases["vector_type"] = "transform_type"
        if kind == "voronoi_texture":
            aliases["distance"] = "metric"
        if kind in ("map_range", "vector_map_range"):
            aliases["interpolation_type"] = "range_type"
        if kind == "vector_map_range":
            aliases["clamp"] = "use_clamp"
        if kind in ("separate_color", "combine_color"):
            aliases["mode"] = "color_type"
        if kind == "mix_color":
            aliases.update(clamp_factor="use_clamp", clamp_result="use_clamp_result")
        skip = {"object_reference", "attribute_type", "color_ramp", "curve_mapping", "data_type", "factor_mode"}
        available = {s["id"] for s in self.definitions.get(kind, {}).get("inputs", ())}
        for original, value in props.items():
            if original in skip:
                applied[original] = None
                continue
            if (item["type"], original) in UNREAD_SETTINGS:
                applied[original] = None
                continue
            key = aliases.get(original, original)
            if (kind, key) in _DEGREE_SETTINGS:
                import math

                value = math.degrees(float(value))
            if key in available:
                if key in ("attribute", "layer_name") and value:
                    import hou

                    if kind == "attribute" and attribute_type in _OBJECT_ATTRIBUTE_TYPES:
                        # Per-object values: Link renders fill this field for every Blender
                        # object and instance (blender_assets._blender_object).
                        from .blend_names import object_attribute_field

                        value = object_attribute_field(attribute_type, value)
                    value = hou.text.encodeAttrib(value)
                if kind == "vertex_color" and key == "layer_name" and not value:
                    value = "__cycles_blender_default_color"
                self.property(item, key, value)
                applied[original] = key
            else:
                self.fail(item, "Unsupported shader setting: " + original)
        if item["type"] == "ShaderNodeMix":
            data_type = props.get("data_type", "RGBA")
            if data_type not in _MIX_DATA_TYPES:
                self.fail(item, "Unsupported Mix data type: " + str(data_type))
            factor_mode = props.get("factor_mode", "UNIFORM")
            if factor_mode not in _MIX_FACTOR_MODES:
                self.fail(item, "Unsupported Mix factor mode: " + str(factor_mode))
            self.parameter(self.nodes[item["id"]], "data_type", _MIX_DATA_TYPES[data_type])
            self.parameter(self.nodes[item["id"]], "factor_mode", _MIX_FACTOR_MODES[factor_mode])
            applied.update(data_type="data_type", factor_mode="factor_mode")
        if "image" in item:
            self.image(item)
        if kind == "color_ramp":
            self.ramp(item)
        if item["type"] in ("ShaderNodeRGB", "ShaderNodeValue", "ShaderNodeNormal"):
            value = item["outputs"][0].get("value")
            if value is not None:
                self.parameter(
                    self.nodes[item["id"]],
                    "direction" if kind == "normal" else "value",
                    value[:3] if isinstance(value, list) else value,
                )

    def ramp(self, item):
        import hou

        ramp, host = item["properties"]["color_ramp"], self.nodes[item["id"]]
        stops = ramp["elements"]
        keys = tuple(s["position"] for s in stops)
        basis = tuple(hou.rampBasis.Linear for _ in stops)
        host.parm("ramp").set(hou.Ramp(basis, keys, tuple(tuple(s["color"][:3]) for s in stops)))
        host.parm("alpha_ramp").set(hou.Ramp(basis, keys, tuple(s["color"][3] for s in stops)))
        for key, value in [
            ("ramp_color_space", ramp["color_mode"]),
            ("ramp_hue_interpolation", ramp["hue_interpolation"]),
            ("ramp_interpolation", ramp["interpolation"]),
        ]:
            self.parameter(host, key, value)

        if "table" in ramp:
            from ..materials.ramps import preview_sources

            if len(ramp["table"]) != 257 or any(len(row) != 4 for row in ramp["table"]):
                self.fail(item, "Color Ramp requires 257 RGBA table entries")
            host.setUserData(
                "cycles_blender_ramp",
                json.dumps(dict(sources=preview_sources(host), table=ramp["table"], interpolate=ramp["interpolate"])),
            )

    def curve(self, item):
        import hou
        import struct
        from ..materials.shader_curves import _snapshot

        mapping, host = item["properties"]["curve_mapping"], self.nodes[item["id"]]
        kind = self.kinds[item["id"]]
        channels = {"float_curve": ("value",), "rgb_curves": ("r", "g", "b", "c"), "vector_curves": ("x", "y", "z")}[
            kind
        ]
        if len(mapping["curves"]) != len(channels):
            self.fail(item, "Unexpected CurveMapping channel count")
        lo = min(p["location"][0] for points in mapping["curves"] for p in points)
        hi = max(p["location"][0] for points in mapping["curves"] for p in points)
        if not hi > lo:
            self.fail(item, "CurveMapping requires an increasing authored range")
        host.parmTuple("curve_domain").set((lo, hi))
        host.parm("curve_extension").set(0 if mapping["extend"] == "EXTRAPOLATED" else 1)

        def bits(v):
            return struct.unpack("i", struct.pack("f", float(v)))[0]

        source_lo, source_hi = mapping.get("clip_min_x", lo), mapping.get("clip_max_x", hi)
        if not source_hi > source_lo:
            source_lo, source_hi = lo, hi
        source = [
            0x43435231,
            1,
            ("float_curve", "rgb_curves", "vector_curves").index(kind),
            int(mapping["extend"] == "EXTRAPOLATED"),
            bits(source_lo),
            bits(source_hi),
            0,
            len(channels),
        ]
        handles = {"AUTO": 0, "AUTO_CLAMPED": 1, "VECTOR": 2}
        for channel, points in zip(channels, mapping["curves"]):
            source.append(len(points))
            for point in points:
                if point["handle_type"] not in handles:
                    self.fail(item, "Unsupported CurveMapping handle: " + point["handle_type"])
                source.extend((bits(point["location"][0]), bits(point["location"][1]), handles[point["handle_type"]]))
            if len(points) > 32:
                points = [points[round(i * (len(points) - 1) / 31)] for i in range(32)]
            # Keep inline editing available. The original Bezier controls remain
            # authoritative while these native UI controls have not been edited.
            host.parm("curve_" + channel).set(
                hou.Ramp(
                    tuple(
                        hou.rampBasis.Linear if p["handle_type"] == "VECTOR" else hou.rampBasis.Bezier for p in points
                    ),
                    tuple((p["location"][0] - lo) / (hi - lo) for p in points),
                    tuple(p["location"][1] for p in points),
                )
            )
        if "table" in mapping:
            if len(mapping["table"]) != 257 or any(len(row) != 3 for row in mapping["table"]):
                self.fail(item, "CurveMapping requires 257 RGB table entries")
            source = [
                0x43435231,
                3,
                ("float_curve", "rgb_curves", "vector_curves").index(kind),
                int(mapping["extend"] == "EXTRAPOLATED"),
                bits(mapping["minimum"]),
                bits(mapping["maximum"]),
                0,
                257,
                *(bits(v) for row in mapping["table"] for v in row),
            ]
        elif any(len(points) > 64 for points in mapping["curves"]):
            self.fail(item, "Legacy CurveMapping record exceeds 64 authored points; re-export in Blender")
        host.setUserData("cycles_blender_curve", json.dumps(dict(source=source, snapshot=_snapshot(host, hou.time()))))

    def parameter_name(self, item, name):
        """The parameter holding an unconnected input's value; the native Mix keeps one per data type."""
        if item["type"] != "ShaderNodeMix":
            return name
        props = item["properties"]
        data_type, mode = props.get("data_type", "RGBA"), props.get("factor_mode", "UNIFORM")
        if name in ("a", "b") and data_type in ("FLOAT", "VECTOR"):
            return name + ("_float" if data_type == "FLOAT" else "_vector")
        if name == "fac" and data_type == "VECTOR" and mode == "NON_UNIFORM":
            return "fac_vector"
        return name

    def sockets(self, item, direction):
        kind, host = self.kinds[item["id"]], self.nodes[item["id"]]
        actual = list(host.inputNames() if direction == "inputs" else host.outputNames())
        definition = self.definitions.get(kind, {})
        candidates = definition.get(direction, ())
        result = {}
        overrides = {}
        if direction == "inputs":
            if kind == "math":
                overrides = {0: "value1", 1: "value2", 2: "value3"}
            elif kind == "vector_math":
                overrides = {0: "vector1", 1: "vector2", 2: "vector3", 3: "scale"}
            elif kind == "mix_closure":
                overrides = {0: "fac", 1: "closure1", 2: "closure2"}
            elif kind == "add_closure":
                overrides = {0: "closure1", 1: "closure2"}
            elif item["type"] == "ShaderNodeMixRGB":
                overrides = {0: "fac", 1: "a", 2: "b"}
            elif item["type"] == "ShaderNodeMix":
                # The native Mix has three ports for every data type (Factor_Float /
                # Factor_Vector, A_Float / A_Vector / A_Color, ...); parameter_name()
                # picks the parameter holding an unconnected input's value.
                overrides = {0: "fac", 1: "fac", 2: "a", 3: "b", 4: "a", 5: "b", 6: "a", 7: "b"}
            elif kind == "color_ramp":
                overrides = {0: "factor"}
            elif kind in ("float_curve", "rgb_curves", "vector_curves"):
                overrides = {0: "fac", 1: "value"}
        elif kind == "color_ramp":
            overrides = {0: "color", 1: "alpha"}
        elif kind in ("float_curve", "rgb_curves", "vector_curves"):
            overrides = {0: "value"}
        for socket in item[direction]:
            index = socket["index"]
            name = overrides.get(index)
            if name is None:
                names = {_norm(socket["name"]), _norm(socket["identifier"])}
                matches = [
                    s.get("host_id", s["id"])
                    for s in candidates
                    if names.intersection({_norm(s["id"]), _norm(s["label"])}) and s.get("host_id", s["id"]) in actual
                ]
                if len(set(matches)) == 1:
                    name = matches[0]
                elif direction == "outputs" and len(actual) == 1:
                    name = actual[0]
                elif direction == "outputs" and item["type"] == "ShaderNodeMix":
                    name = "result" if "result" in actual else None
            if name in actual:
                result[index] = name
            elif direction == "outputs" and (item["id"], index) not in self.used:
                continue
            elif direction == "inputs" and socket["type"] == "SHADER" and "source" not in socket:
                continue
            elif direction == "inputs" and not socket.get("enabled", True) and "source" not in socket:
                continue
            else:
                self.fail(
                    item, "Unmapped " + direction + " socket: " + socket["name"] + " [" + socket["identifier"] + "]"
                )
        return result

    def build(self):
        if self.record.get("unsupported"):
            raise BlendMaterialError(self.record["name"] + ": " + str(self.record["unsupported"]))
        for item in self.record["nodes"]:
            kind = node_kind(item, self.world)
            if kind is None or (kind != "color_ramp" and kind not in self.definitions):
                self.fail(item, "Unsupported connected Blender node type: " + item["type"])
            # World-space ports get typed axis adapters while wiring (see
            # _BLENDER_TO_HOUDINI). Blender's object random comes from Blender's
            # object/instance identity, which a Houdini instance does not have.
            used_names = {s["name"] for s in item["outputs"] if (item["id"], s["index"]) in self.used}
            if kind == "object_info" and "Random" in used_names:
                self.warn(item, OBJECT_INFO_RANDOM_WARNING)
            self.kinds[item["id"]] = kind
            self.nodes[item["id"]] = self.host.createNode("cycles::" + kind + "::1", _name(item["name"]))
        for item in self.record["nodes"]:
            self.properties(item)
            self.socket_maps[item["id"]] = {d: self.sockets(item, d) for d in ("inputs", "outputs")}
        for item in self.record["nodes"]:
            host, kind = self.nodes[item["id"]], self.kinds[item["id"]]
            closure = kind.endswith("_bsdf") or kind in (
                "background_shader",
                "emission",
                "subsurface_scattering",
                "absorption_volume",
                "scatter_volume",
                "principled_volume",
            )
            definition = self.definitions.get(kind, {})
            for socket in item["inputs"]:
                name = self.socket_maps[item["id"]]["inputs"].get(socket["index"])
                if name is None:
                    continue
                native = next((s for s in definition.get("inputs", ()) if s.get("host_id", s["id"]) == name), {})
                transform = closure and native.get("type") == "color"
                space = self.input_space(item, name, native)
                if (
                    self.world
                    and "source" not in socket
                    and _WORLD_COORDINATE_LINKS.intersection(native.get("default_links") or ())
                ):
                    endpoint = self.direction()
                    host.setNamedInput(name, endpoint[0], endpoint[1])
                    continue
                if space and "value" in socket:  # a Blender-world constant
                    socket = dict(
                        socket,
                        value=_axes_value(socket["value"], _BLENDER_TO_HOUDINI, encoded=space == "encoded_world"),
                    )
                if "source" in socket:
                    endpoint = self.endpoint(socket["source"])
                    endpoint = self.adapt(endpoint, self.output_space(socket["source"]), space)
                    if transform:
                        endpoint = self.color(endpoint, self.to_working, "to_working")
                    host.setNamedInput(name, endpoint[0], endpoint[1])
                elif "value" in socket and (
                    socket.get("explicit_default") or socket.get("value_type", socket["type"]) != socket["type"]
                ):
                    endpoint = self.constant(socket)
                    if transform:
                        endpoint = self.color(endpoint, self.to_working, "to_working")
                    host.setNamedInput(name, endpoint[0], endpoint[1])
                elif "value" in socket and socket["type"] != "SHADER" and not native.get("default_links"):
                    value = socket["value"]
                    if transform:
                        value = [sum(row[c] * value[c] for c in range(3)) for row in self.to_working]
                    value = value[:3] if isinstance(value, list) and len(value) == 4 else value
                    self.parameter(host, self.parameter_name(item, name), value)
        for domain, terminal in self.record["outputs"].items():
            if "source" not in terminal:
                raise BlendMaterialError("Material output must have a connected shader: " + domain)
            endpoint = self.endpoint(terminal["source"])
            if domain == "displacement":  # Cycles displaces by a world-space vector
                endpoint = self.adapt(endpoint, self.output_space(terminal["source"]), "world")
            self.output.setNamedInput(domain, endpoint[0], endpoint[1])
        if not self.world:
            settings = self.record.get("settings", {})
            method = settings.get("displacement_method", "BUMP")
            self.host.parm("cycles_displacement_method").set(
                {"BUMP": "bump", "DISPLACEMENT": "true", "BOTH": "both"}[method]
            )
            _apply_settings(self.host, settings)
        if has_layout(self.record):
            _Layout(self.host, self.record, self.nodes, self.output).place()
        else:  # older caches, the no-node-tree fallback and the default surface
            self.host.layoutChildren()
        # Run the same native extraction used by actual rendering now. A
        # created but invalid graph is never returned as a usable material.
        from ..materials import material_graph

        material_graph(self.host)
        return self.host.path()


# Blender Material > Settings record values -> Cycles Material parameters.
# Records from caches written before these settings existed omit them; the
# parameters then keep their defaults, which equal Blender's.
_SETTING_TOKENS = {
    "emission_sampling": (
        "cycles_emission_sampling",
        {"NONE": "none", "AUTO": "auto", "FRONT": "front", "BACK": "back", "FRONT_BACK": "front_back"},
    ),
    "volume_sampling": (
        "cycles_volume_sampling",
        {"DISTANCE": "distance", "EQUIANGULAR": "equiangular", "MULTIPLE_IMPORTANCE": "multiple_importance"},
    ),
    "volume_interpolation": ("cycles_volume_interpolation", {"LINEAR": "linear", "CUBIC": "cubic"}),
}
_SETTING_VALUES = {
    "use_transparent_shadow": "cycles_transparent_shadows",
    "use_bump_map_correction": "cycles_bump_map_correction",
    "volume_step_rate": "cycles_volume_step_rate",
}


def _apply_settings(host, settings):
    for key, (name, tokens) in _SETTING_TOKENS.items():
        if key in settings:
            if settings[key] not in tokens:
                raise BlendMaterialError("Unsupported material setting " + key + ": " + str(settings[key]))
            host.parm(name).set(tokens[settings[key]])
    for key, name in _SETTING_VALUES.items():
        if key in settings:
            value = settings[key]
            host.parm(name).set(float(value) if key == "volume_step_rate" else int(bool(value)))


def import_materials(records, asset_root, parent=None, diagnostics=None):
    """Return material-key → absolute native material path; fail atomically."""
    import hou
    from .. import shader_catalogue

    parent = parent or hou.node("/mat")
    definitions = {
        d["engine_type"]: d
        for d in shader_catalogue()["definitions"]
        if d.get("surface_graph_adapter") or d.get("volume_graph_adapter")
    }
    builders, result = [], {}
    try:
        for record in records:
            builder = _Builder(record, asset_root, parent, definitions)
            builders.append(builder)
            result[record["key"]] = builder.build()
        # Translated-but-approximate graphs: reported to the caller and kept on
        # the material itself (library imports pass no diagnostics list).
        for builder in builders:
            if builder.warnings:
                builder.host.setUserData(
                    "cycles_blender_material_warnings", json.dumps([w["message"] for w in builder.warnings])
                )
                if diagnostics is not None:
                    diagnostics.extend(builder.warnings)
        return result
    except Exception as error:
        for builder in reversed(builders):
            builder.host.destroy()
        if diagnostics is not None:
            diagnostics.append(dict(code="blender.material.unsupported", message=str(error), severity="error"))
        if isinstance(error, BlendMaterialError):
            raise
        raise BlendMaterialError(str(error)) from error


def import_world_material(record, asset_root, parent=None, diagnostics=None):
    """Build one Blender World graph record (blend_world_export) as a Cycles World Material.

    Same contract as import_materials for one record: returns the builder's path, warnings
    go to `diagnostics` and onto the node, any failure destroys the partial builder and
    raises BlendMaterialError. Only nodes with a native world graph adapter are accepted;
    Background and Sky Texture are accepted here and nowhere else. Default view-direction
    inputs (Environment/Sky Texture Vector, procedural textures' Generated) are wired from
    Geometry Position converted to Blender axes, and Texture Coordinate direction outputs
    are converted the same way, so the graph computes with Blender-world directions.
    """
    import hou
    from .. import shader_catalogue

    parent = parent or hou.node("/mat")
    definitions = {d["engine_type"]: d for d in shader_catalogue()["definitions"] if d.get("world_graph_adapter")}
    builder = None
    try:
        builder = _Builder(record, asset_root, parent, definitions, world=True)
        path = builder.build()
        if builder.warnings:
            builder.host.setUserData(
                "cycles_blender_material_warnings", json.dumps([w["message"] for w in builder.warnings])
            )
            if diagnostics is not None:
                diagnostics.extend(builder.warnings)
        return path
    except Exception as error:
        if builder is not None:
            builder.host.destroy()
        if diagnostics is not None:
            diagnostics.append(dict(code="blender.material.unsupported", message=str(error), severity="error"))
        if isinstance(error, BlendMaterialError):
            raise
        raise BlendMaterialError(str(error)) from error
