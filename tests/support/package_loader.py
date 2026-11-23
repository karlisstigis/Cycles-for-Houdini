# SPDX-License-Identifier: Apache-2.0
"""Canonical source loading for host fakes, without executing compatibility shims."""

import importlib
import importlib.util
import json
import sys
from types import ModuleType

from .paths import BLENDER, PACKAGE, product_source

ALIASES = json.loads((PACKAGE / "_module_aliases.json").read_text())
LEGACY = {legacy: canonical for canonical, legacy in ALIASES.items() if legacy}
LEGACY["materials"] = "materials"


def import_product(legacy):
    """Import source implementations normally, including in standalone Blender."""
    if str(PACKAGE.parent) not in sys.path:
        sys.path.insert(0, str(PACKAGE.parent))
    return importlib.import_module("cycles_houdini." + LEGACY[legacy])


def import_blender(name):
    """Import a module of the Blender component (blender/cycles_blender, GPL-3.0-or-later), with
    the Houdini package's python3.13libs for the shared Apache-2.0 modules it imports. For tests
    that drive the exporter in Blender's Python; never from a test that imports hou."""
    for path in (PACKAGE.parent, BLENDER):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    return importlib.import_module("cycles_blender." + name)


def prepare_package(package):
    """Place a fake host's injected flat dependencies in their owning namespaces."""
    package.__path__ = [str(PACKAGE)]
    sys.modules[package.__name__] = package
    for group in ("core", "blender", "scene", "materials", "ipr", "viewer", "rop"):
        name = package.__name__ + "." + group
        namespace = sys.modules.get(name)
        if namespace is None:
            namespace = ModuleType(name)
            namespace.__path__ = [str(PACKAGE / group)]
            sys.modules[name] = namespace
        setattr(package, group, namespace)
    for legacy, canonical in LEGACY.items():
        if "." not in canonical:
            continue
        value = sys.modules.get(package.__name__ + "." + legacy, getattr(package, legacy, None))
        if value is None:
            continue
        group, _, child = canonical.rpartition(".")
        setattr(sys.modules[package.__name__ + "." + group], child, value)
        sys.modules[package.__name__ + "." + canonical] = value
    return package


def load_module(package, legacy):
    """Return the registered canonical object, just as ordinary imports do."""
    prepare_package(package)
    canonical = LEGACY[legacy]
    name = package.__name__ + "." + canonical
    source = product_source(legacy + ".py")
    locations = [str(source.parent)] if source.name == "__init__.py" else None
    spec = importlib.util.spec_from_file_location(name, source, submodule_search_locations=locations)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    sys.modules[package.__name__ + "." + legacy] = module
    setattr(package, legacy, module)
    parent, _, child = name.rpartition(".")
    setattr(sys.modules[parent], child, module)
    return module
