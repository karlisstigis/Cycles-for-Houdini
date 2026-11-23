# SPDX-License-Identifier: Apache-2.0
"""Pure boundaries for native group authoring, identity, palette and foreign networks."""

from contextlib import nullcontext
from support.package_loader import load_module
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


class Color:
    def __init__(self, value):
        self.value = tuple(value)

    def rgb(self):
        return self.value


class Template:
    def __init__(self, name, menu=()):
        self.key = name
        self.menu = menu
        self.default = None

    def name(self):
        return self.key

    def setDefaultValue(self, value):
        self.default = list(value)

    def menuItems(self):
        return self.menu


class Group:
    def __init__(self):
        self.entries = {}

    def find(self, name):
        return self.entries.get(name)

    def append(self, item):
        self.entries[item.name()] = item

    def replace(self, name, item):
        self.entries[name] = item


class Parm:
    def __init__(self, value=None, menu=()):
        self.value = value
        self.menu = menu

    def set(self, value):
        self.value = self.menu[value] if self.menu and isinstance(value, int) else value

    def evalAsString(self):
        return str(self.value)

    def parmTemplate(self):
        return Template("parm", self.menu)


class Node:
    def __init__(self, kind, name="node", parent=None):
        self.kind = kind
        self.key = name
        self.owner = parent
        self.items = []
        self.data = {}
        self.color_value = Color((0.3, 0.4, 0.5))
        self.group = Group()
        self.language = None
        self.language_writes = 0
        self.create_calls = []
        self.tuples = {}
        self.parameters = {
            "parmname": Parm("parm"),
            "parmlabel": Parm("Label"),
            "connectorkind": Parm("input", ("input", "output", "inputoutput")),
            "parmtype": Parm("float", ("float", "bsdf", "color", "vector", "float3", "normal", "point", "direction")),
            "floatdef": Parm(0.0),
        }
        if parent is not None:
            parent.items.append(self)

    def type(self):
        return SimpleNamespace(name=lambda: self.kind)

    def parent(self):
        return self.owner

    def children(self):
        return tuple(self.items)

    def node(self, name):
        return next((node for node in self.items if node.key == name), None)

    def destroy(self):
        self.owner.items.remove(self)

    def userData(self, name):
        return self.data.get(name)

    def setUserData(self, name, value):
        self.data[name] = value

    def setColor(self, value):
        self.color_value = value

    def color(self):
        return self.color_value

    def setShaderLanguageName(self, value):
        self.language = value
        self.language_writes += 1
        return True

    def parmTemplateGroup(self):
        return self.group

    def setParmTemplateGroup(self, value):
        self.group = value

    def parm(self, name):
        return self.parameters[name]

    def evalParm(self, name):
        return self.parameters[name].value

    def parmTuple(self, name):
        if name not in self.tuples:
            self.tuples[name] = SimpleNamespace(set=lambda value: self.tuples.update({name: tuple(value)}))
        return self.tuples[name]

    def createNode(self, kind, name, **kwargs):
        self.create_calls.append((kind, name, kwargs))
        return Node(kind, name, self)


class AuthoringTest(unittest.TestCase):
    def setUp(self):
        hou = ModuleType("hou")
        hou.Color = Color
        hou.VopNode = Node
        hou.undos = SimpleNamespace(group=lambda label: nullcontext())
        hou.properties = SimpleNamespace(parmTemplate=lambda category, name: Template(name))
        package = ModuleType("authoring_test")
        package.__path__ = []
        light = ModuleType("authoring_test.light_shaders")
        light._allowed = lambda: {"cycles/emission/1"}
        self.modules = patch.dict(
            sys.modules, {"hou": hou, "authoring_test": package, "authoring_test.light_shaders": light}
        )
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.m = self.load("materials")
        sys.modules["authoring_test.materials"] = self.m
        self.m._definitions = lambda: {
            "cycles::diffuse_bsdf::1": dict(
                id="cycles/diffuse_bsdf/1",
                surface_graph_adapter=True,
                volume_graph_adapter=False,
                world_graph_adapter=False,
            ),
            "cycles::emission::1": dict(
                id="cycles/emission/1", surface_graph_adapter=True, volume_graph_adapter=False, world_graph_adapter=True
            ),
            "cycles::background_shader::1": dict(
                id="cycles/background_shader/1",
                surface_graph_adapter=False,
                volume_graph_adapter=False,
                world_graph_adapter=True,
            ),
        }
        self.mat = Node("mat")
        self.builder = Node(self.m.BUILDER, parent=self.mat)

    def load(self, name):
        return load_module(sys.modules["authoring_test"], name)

    def test_builtin_group_has_no_custom_type_or_initial_defaults(self):
        group = self.m.create_subnet(self.builder, "owned")
        self.assertEqual(group.kind, "subnet")
        self.assertEqual(group.language, "Cycles")
        self.assertEqual(group.children(), ())
        self.assertEqual(self.builder.create_calls[-1][2], dict(run_init_scripts=False, exact_type_name=True))
        self.assertRegex(group.userData("cycles_graph_id"), r"^[0-9a-f]{32}$")
        self.assertEqual(group.color().rgb(), tuple(v / 255 for v in (55, 71, 37)))
        mask = group.group.find("tabmenumask").default[0].split()
        self.assertIn("cycles::diffuse_bsdf::1", mask)
        self.assertIn("subnetconnector", mask)
        self.assertNotIn("cycles::background_shader::1", mask)

    def test_world_and_light_group_masks_keep_their_own_shader_domains(self):
        world = self.m.create_subnet(Node(self.m.WORLD_BUILDER, parent=self.mat))
        light = self.m.create_subnet(Node(self.m.LIGHT_BUILDER, parent=self.mat))
        self.assertIn("cycles::background_shader::1", world.group.find("tabmenumask").default[0].split())
        light_mask = light.group.find("tabmenumask").default[0].split()
        self.assertIn("cycles::emission::1", light_mask)
        self.assertNotIn("cycles::diffuse_bsdf::1", light_mask)

    def test_typed_connector_has_stable_identity_and_interface_palette(self):
        group = self.m.create_subnet(self.builder)
        port = self.m.create_connector(group, "gain", default=2.0)
        self.assertEqual(port.kind, "subnetconnector")
        self.assertEqual(port.evalParm("floatdef"), 2.0)
        self.assertEqual(port.color().rgb(), tuple(v / 255 for v in (29, 29, 29)))
        self.assertRegex(port.userData("cycles_graph_id"), r"^[0-9a-f]{32}$")
        self.assertNotEqual(port.userData("cycles_graph_id"), group.userData("cycles_graph_id"))
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.m.create_connector(group, "gain")

    def test_initializers_preserve_existing_identity_artist_color_and_mask(self):
        group = self.m.create_subnet(self.builder)
        port = self.m.create_connector(group, "port")
        identities = [node.userData("cycles_graph_id") for node in (group, port)]
        for node in (group, port):
            node.setColor(Color((0.9, 0.2, 0.1)))
        group.group.find("tabmenumask").default = ["artist-owned-mask"]
        self.m.initialize_subnet(group)
        self.m.initialize_connector(port)
        self.assertEqual([node.userData("cycles_graph_id") for node in (group, port)], identities)
        self.assertTrue(all(node.color().rgb() == (0.9, 0.2, 0.1) for node in (group, port)))
        self.assertEqual(group.language_writes, 1)
        self.assertEqual(group.group.find("tabmenumask").default, ["artist-owned-mask"])

    def test_foreign_native_subnets_are_rejected_before_any_mutation(self):
        foreign = Node("subnet", parent=self.mat)
        shader = Node("cycles::emission::1", parent=foreign)
        for operation in (
            lambda: self.m.create_subnet(foreign),
            lambda: self.m.create_connector(foreign, "port"),
            lambda: self.m.promote_parameter(foreign, shader, "value"),
        ):
            with self.assertRaises(ValueError):
                operation()
        self.assertEqual(foreign.create_calls, [])
        self.assertEqual(foreign.data, {})

    def test_shelf_initializer_removes_only_known_fresh_stock_placeholders(self):
        group = Node("subnet", parent=self.builder)
        Node("subinput", "subinput1", group)
        Node("suboutput", "suboutput1", group)
        artist = Node("subinput", "artist_input", group)
        self.m.initialize_subnet(group, fresh_defaults=True)
        self.assertEqual(group.children(), (artist,))

    def test_preview_accepts_builtin_nodes_only_below_verified_cycles_owner(self):
        preview = self.load("network_preview")
        group = self.m.create_subnet(self.builder)
        port = self.m.create_connector(group, "port")
        self.assertIs(preview._builder(group), self.builder)
        self.assertIs(preview._builder(port), self.builder)
        foreign = Node("subnet", parent=self.mat)
        foreign_port = Node("subnetconnector", parent=foreign)
        self.assertIsNone(preview._builder(foreign))
        self.assertIsNone(preview._builder(foreign_port))

    def test_foreign_shelf_context_is_rejected_before_native_generic_creation(self):
        generic = Mock()
        tools = ModuleType("toolutils")
        vops = ModuleType("voptoolutils")
        tools.activePane = lambda kwargs: SimpleNamespace(pwd=lambda: self.mat)
        vops.genericTool = generic
        with patch.dict(sys.modules, {"toolutils": tools, "voptoolutils": vops}):
            with self.assertRaises(ValueError):
                self.m.create_subnet_tool({})
            with self.assertRaises(ValueError):
                self.m.create_connector_tool({})
        generic.assert_not_called()


if __name__ == "__main__":
    unittest.main()
