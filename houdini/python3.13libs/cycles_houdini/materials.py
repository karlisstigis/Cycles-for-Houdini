# SPDX-License-Identifier: Apache-2.0
"""Legacy file entrypoint for Houdini importers that prefer files to packages."""

from importlib import import_module as _import_module, util as _util
from pathlib import Path as _Path
import sys as _sys

_source = _Path(__file__).with_name("materials") / "__init__.py"
_name = (__package__ or "cycles_houdini") + ".materials"
_parent = _import_module(_name.rpartition(".")[0])
_implementation = getattr(_parent, "materials", None)
if _Path(getattr(_implementation, "__file__", "")) != _source:
    _spec = _util.spec_from_file_location(_name, _source, submodule_search_locations=[str(_source.parent)])
    _implementation = _util.module_from_spec(_spec)
    _sys.modules[_name] = _implementation
    try:
        _spec.loader.exec_module(_implementation)
    except BaseException:
        _sys.modules.pop(_name, None)
        raise
    _parent.materials = _implementation

# The loader's original module reference remains useful to direct-file callers.
__path__ = _implementation.__path__
if __spec__ is not None:
    __spec__.submodule_search_locations = __path__


def __getattr__(name):
    return getattr(_implementation, name)


def __dir__():
    return dir(_implementation)


_sys.modules[__name__] = _implementation
