# SPDX-License-Identifier: GPL-3.0-or-later
# Blender component of Cycles for Houdini (see blender/README.md). This file is one of the scripts
# that run inside Blender and use its Python API, so it is published under the GNU General Public
# License, version 3 or later (blender/LICENSE). The Houdini integration (Apache-2.0) never imports
# it; the two sides exchange only files, command-line arguments and pipe messages.
"""Blender-side World export (no Houdini imports; runs in Blender's process).

One World datablock becomes one record (blend-import.md: Worlds):

``kind == "simple"`` when every rule of SIMPLE_RULES holds. The record then carries
``simple``: the HDRI image (blend_material_export._Images record), the Background
Strength, the tint colour and the Mapping Z rotation, so Houdini can build a native
Environment Light. Otherwise ``reasons`` lists every rule that failed, and the world
is exported as a node graph for a Cycles World Material: ``kind == "material"`` with
``material`` (the same record shape as a material, outputs ``surface`` / ``volume``
from the World Output), or ``kind == "unsupported"`` with ``unsupported`` (the
exporter's reason) when even that graph cannot be exported.

Accepted simple chains (reroutes and muted nodes are followed as Blender does):

    [Texture Coordinate.Generated ->] [Mapping ->] Environment Texture.Color
        [-> one constant multiply] -> Background.Color -> World Output.Surface

The constant multiply is exactly one of: Mix (Color, Multiply, factor 1, no clamp),
legacy MixRGB (Multiply, factor 1, no clamp) or Vector Math (Multiply), whose other
operand is an unconnected socket or an RGB node. A Mapping node needs a Texture
Coordinate Generated input: an unconnected Mapping Vector is a constant (0, 0, 0)
in Cycles, not the view direction.

Drivers (for example Poly Haven's rotation and strength sliders, driven from custom
properties) are evaluated first: the world is made the scene world for one depsgraph
update, which writes driven values back to the datablock.
"""

from . import blend_material_export as exporter

# Rule id -> the condition, as shown to the artist when it fails.
SIMPLE_RULES = {
    "nodes": "The world uses nodes",
    "output": "One active World Output (All or Cycles)",
    "volume": "Nothing is connected to World Output > Volume",
    "background": "World Output > Surface comes from a Background node",
    "strength": "Background Strength is a constant",
    "color": "Background Color is an Environment Texture, or one multiplied by a single constant colour",
    "texture": "The Environment Texture is equirectangular, linear, with a still image file",
    "vector": "Environment Texture Vector is unconnected, Texture Coordinate Generated, or Generated through one Mapping",
    "mapping": "The Mapping node is Point type with location 0, scale 1 and rotation about Z only",
    "visibility": "World ray visibility (Camera, Diffuse, Glossy, Transmission, Volume Scatter, Shadow) is all on",
}
_VISIBILITY = ("camera", "diffuse", "glossy", "transmission", "scatter", "shadow")
_EPSILON = 1e-6


class _Classifier:
    def __init__(self):
        self.reasons = []

    def fail(self, rule, detail):
        self.reasons.append(dict(rule=rule, message=SIMPLE_RULES[rule] + ": " + detail))
        return None

    @staticmethod
    def source(socket, seen=None):
        """(node, output socket) feeding `socket` after reroutes and muted nodes; None when unconnected.

        A muted node without a bypass for the used output yields ("muted", node).
        """
        seen = set() if seen is None else seen
        links = [link for link in socket.links if link.is_valid and not getattr(link, "is_muted", False)]
        if not links:
            return None
        output = links[0].from_socket
        node = output.node
        if node.as_pointer() in seen:
            return ("cycle", node)
        seen.add(node.as_pointer())
        if node.bl_idname == "NodeReroute":
            return _Classifier.source(node.inputs[0], seen)
        if node.mute:
            for link in node.internal_links:
                if link.to_socket == output:
                    return _Classifier.source(link.from_socket, seen)
            return ("muted", node)
        return node, output

    def constant_colour(self, socket):
        """RGB of an unconnected colour/vector socket or an RGB node; None otherwise."""
        found = self.source(socket)
        if found is None:
            value = list(socket.default_value)
            return [float(v) for v in value[:3]]
        node, output = found
        if not isinstance(node, str) and node.bl_idname == "ShaderNodeRGB":
            return [float(v) for v in list(output.default_value)[:3]]
        return None

    @staticmethod
    def _is_texture(found):
        return (
            found is not None
            and not isinstance(found[0], str)
            and found[0].bl_idname == "ShaderNodeTexEnvironment"
            and found[1].identifier == "Color"
        )

    def multiply(self, node):
        """(texture node, tint) of an accepted constant multiply node, or None (reason recorded)."""
        kind = node.bl_idname
        if kind == "ShaderNodeMix":
            if node.data_type != "RGBA" or node.blend_type != "MULTIPLY":
                return self.fail("color", f"Mix '{node.name}' is not a Color Multiply")
            if node.clamp_result:
                return self.fail("color", f"Mix '{node.name}' clamps its result")
            inputs = {socket.identifier: socket for socket in node.inputs}
            factor, operands = inputs["Factor_Float"], (inputs["A_Color"], inputs["B_Color"])
        elif kind == "ShaderNodeMixRGB":
            if node.blend_type != "MULTIPLY":
                return self.fail("color", f"MixRGB '{node.name}' is not Multiply")
            if node.use_clamp:
                return self.fail("color", f"MixRGB '{node.name}' clamps its result")
            factor, operands = node.inputs[0], (node.inputs[1], node.inputs[2])
        elif kind == "ShaderNodeVectorMath":
            if node.operation != "MULTIPLY":
                return self.fail("color", f"Vector Math '{node.name}' is not Multiply")
            factor, operands = None, (node.inputs[0], node.inputs[1])
        else:
            return self.fail("color", f"'{node.name}' ({kind}) is not an accepted multiply node")
        if factor is not None and (self.source(factor) is not None or abs(factor.default_value - 1.0) > _EPSILON):
            return self.fail("color", f"'{node.name}' Factor is not a constant 1")
        found = [self.source(socket) for socket in operands]
        textures = [i for i, value in enumerate(found) if self._is_texture(value)]
        if len(textures) != 1:
            return self.fail("color", f"'{node.name}' does not multiply exactly one Environment Texture Color")
        tint = self.constant_colour(operands[1 - textures[0]])
        if tint is None:
            return self.fail("color", f"'{node.name}' multiplies the texture by a non-constant input")
        return found[textures[0]][0], tint

    def vector(self, texture):
        """Mapping Z rotation (radians) of the texture's Vector chain, or None (reason recorded)."""
        found = self.source(texture.inputs["Vector"])
        if found is None:
            return 0.0
        node, output = found
        if isinstance(node, str):
            return self.fail("vector", f"muted node '{output.name}' has no bypass")
        if node.bl_idname == "ShaderNodeTexCoord":
            if output.identifier != "Generated":
                return self.fail("vector", f"Texture Coordinate {output.name} is used instead of Generated")
            return 0.0
        if node.bl_idname != "ShaderNodeMapping":
            return self.fail("vector", f"'{node.name}' ({node.bl_idname}) drives the Vector")
        mapping = node
        rotation = self.mapping(mapping)
        found = self.source(mapping.inputs["Vector"])
        if found is None:
            return self.fail("vector", f"Mapping '{mapping.name}' Vector is unconnected (a constant 0 in Cycles)")
        node, output = found
        if isinstance(node, str) or node.bl_idname != "ShaderNodeTexCoord" or output.identifier != "Generated":
            name = output.name if isinstance(node, str) else node.name
            return self.fail(
                "vector", f"Mapping '{mapping.name}' Vector does not come from Texture Coordinate Generated ({name})"
            )
        return rotation

    def mapping(self, node):
        if node.vector_type != "POINT":
            return self.fail("mapping", f"'{node.name}' is {node.vector_type.title()} type")
        sockets = {name: node.inputs[name] for name in ("Location", "Rotation", "Scale")}
        driven = [name for name, socket in sockets.items() if self.source(socket) is not None]
        if driven:
            return self.fail("mapping", f"'{node.name}' {', '.join(driven)} is connected")
        location = list(sockets["Location"].default_value)
        rotation = list(sockets["Rotation"].default_value)
        scale = list(sockets["Scale"].default_value)
        if any(abs(v) > _EPSILON for v in location):
            return self.fail("mapping", f"'{node.name}' Location is {location}")
        if any(abs(v - 1.0) > _EPSILON for v in scale):
            return self.fail("mapping", f"'{node.name}' Scale is {scale}")
        if abs(rotation[0]) > _EPSILON or abs(rotation[1]) > _EPSILON:
            return self.fail("mapping", f"'{node.name}' rotates about X or Y ({rotation})")
        return float(rotation[2])

    def texture(self, node):
        problems = []
        if node.projection != "EQUIRECTANGULAR":
            problems.append("projection is " + node.projection.replace("_", " ").title())
        if node.interpolation != "Linear":
            problems.append(f"interpolation is {node.interpolation}")
        image = node.image
        if image is None:
            problems.append("no image")
        elif image.source != "FILE":
            problems.append(f"image source is {image.source.title()}")
        elif getattr(image, "use_multiview", False):
            problems.append("the image is multiview")
        if problems:
            return self.fail("texture", f"'{node.name}' " + ", ".join(problems))
        return node

    def classify(self, world):
        """The simple-world fields, or None with self.reasons naming every failed rule."""
        visibility = getattr(world, "cycles_visibility", None)
        if visibility is not None:
            off = [name for name in _VISIBILITY if not getattr(visibility, name, True)]
            if off:
                self.fail("visibility", "off for " + ", ".join(off))
        tree = world.node_tree if getattr(world, "use_nodes", True) else None
        if tree is None:
            return self.fail("nodes", "the world has no node tree (a constant colour)")
        outputs = [
            n
            for n in tree.nodes
            if n.bl_idname == "ShaderNodeOutputWorld" and n.is_active_output and n.target in ("ALL", "CYCLES")
        ]
        outputs = [n for n in outputs if n.target == "CYCLES"] or outputs
        if len(outputs) != 1:
            return self.fail("output", f"{len(outputs)} found")
        output = outputs[0]
        if self.source(output.inputs["Volume"]) is not None:
            self.fail("volume", "a volume shader is connected")
        found = self.source(output.inputs["Surface"])
        if found is None or isinstance(found[0], str) or found[0].bl_idname != "ShaderNodeBackground":
            what = "nothing" if found is None else getattr(found[0], "name", found[0])
            return self.fail("background", f"found {what}")
        background = found[0]
        strength = background.inputs["Strength"]
        if self.source(strength) is not None:
            self.fail("strength", "a node drives Strength")
        found = self.source(background.inputs["Color"])
        if found is None:
            return self.fail("color", "Color is a constant colour")
        if isinstance(found[0], str):
            return self.fail("color", f"muted node '{found[1].name}' has no bypass")
        if self._is_texture(found):
            texture, tint = found[0], [1.0, 1.0, 1.0]
        else:
            accepted = self.multiply(found[0])
            if accepted is None:
                return None
            texture, tint = accepted
        usable = self.texture(texture) is not None
        rotation = self.vector(texture)
        if self.reasons or not usable or rotation is None:
            return None
        return dict(
            strength=float(strength.default_value), tint=[float(v) for v in tint], rotation_z=rotation, node=texture
        )


def evaluate_drivers(world):
    """Evaluate the world's drivers once; return warnings for drivers Blender could not evaluate."""
    import bpy

    scene = bpy.context.scene
    previous = scene.world
    try:
        scene.world = world
        bpy.context.evaluated_depsgraph_get().update()
    finally:
        scene.world = previous
    warnings = []
    for owner in (world, world.node_tree):
        drivers = getattr(getattr(owner, "animation_data", None), "drivers", None) or ()
        for curve in drivers:
            if not curve.driver.is_valid:
                warnings.append(
                    f"Driver {curve.data_path} of {world.name} could not be evaluated "
                    "(Python expressions are disabled in the export); its stored value is used"
                )
    return warnings


def _constant_record(world, color):
    """A node-less World renders World.color: one Background node (like Cycles' BlenderSync)."""
    background = dict(
        id="n0",
        name="Background",
        type="ShaderNodeBackground",
        properties={},
        inputs=[
            dict(
                index=0,
                identifier="Color",
                name="Color",
                type="RGBA",
                enabled=True,
                value=[float(c) for c in world.color] + [1.0],
                value_type="RGBA",
            ),
            dict(index=1, identifier="Strength", name="Strength", type="VALUE", enabled=True, value=1.0),
        ],
        outputs=[dict(index=0, identifier="Background", name="Background", type="SHADER", enabled=True)],
        location=[0.0, 0.0],
    )
    terminal = dict(source=dict(node="n0", socket="Background", name="Background", index=0))
    return dict(
        key=world_key(world),
        name=world.name,
        nodes=[background],
        outputs=dict(surface=terminal),
        settings={},
        color_management=color,
    )


def world_key(world):
    return exporter.material_key(world).replace("blender-material-", "blender-world-", 1)


def _graph_record(world, images, color):
    tree = world.node_tree if getattr(world, "use_nodes", True) else None
    if tree is None:
        return _constant_record(world, color)
    outputs = [
        n
        for n in tree.nodes
        if n.bl_idname == "ShaderNodeOutputWorld" and n.is_active_output and n.target in ("ALL", "CYCLES")
    ]
    outputs = [n for n in outputs if n.target == "CYCLES"] or outputs
    if len(outputs) != 1:
        raise exporter.BlendMaterialExportError("World needs one active Cycles World Output: " + world.name)
    graph = exporter._Graph(images)
    terminals = {}
    for name in ("Surface", "Volume"):
        socket = outputs[0].inputs.get(name)
        if socket is not None and socket.is_linked:
            terminals[name.lower()] = graph.input(socket, ())
    if not terminals:
        raise exporter.BlendMaterialExportError("World Output is unconnected: " + world.name)
    return dict(
        key=world_key(world),
        name=world.name,
        nodes=graph.nodes,
        outputs=terminals,
        settings={},
        color_management=color,
        **graph.layout(outputs[0]),
    )


def _color_management():
    import PyOpenColorIO as ocio

    working = ocio.GetCurrentConfig().getColorSpace(ocio.ROLE_SCENE_LINEAR)
    if working is None:
        raise exporter.BlendMaterialExportError("Blender OCIO configuration has no scene_linear role")
    return dict(colorspace=working.getName(), color_interop_id=exporter._color_identity(working))


def export_world(world, output_dir, textures="in_place"):
    """One world record (module docstring). Raises only for an unusable output_dir or OCIO setup."""
    images = exporter._Images(output_dir, textures)
    color = _color_management()
    warnings = evaluate_drivers(world) if world.node_tree is not None or world.animation_data else []
    visibility = getattr(world, "cycles_visibility", None)
    record = dict(
        name=world.name,
        key=world_key(world),
        reasons=[],
        warnings=warnings,
        color_management=color,
        # Ray visibility is a ROP setting in Houdini (World Material route: reported, not set).
        visibility={name: bool(getattr(visibility, name, True)) for name in _VISIBILITY},
    )
    classifier = _Classifier()
    simple = classifier.classify(world)
    if simple is not None:
        try:
            image = images.export(simple.pop("node").image)
        except Exception as error:  # noqa: BLE001 - an unexportable image fails the texture rule
            classifier.fail("texture", str(error) or type(error).__name__)
        else:
            record.update(kind="simple", simple=dict(simple, image=image))
            record["dependencies"] = images.dependencies
            return record
    record["reasons"] = classifier.reasons
    try:
        record.update(kind="material", material=_graph_record(world, images, color))
    except Exception as error:  # noqa: BLE001 - the reason is the verdict shown to the artist
        record.update(kind="unsupported", unsupported=str(error) or type(error).__name__)
    record["dependencies"] = images.dependencies
    return record
