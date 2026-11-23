# SPDX-License-Identifier: Apache-2.0
"""Owned Material SOP literals and read-only promoted-channel evaluation.

Material SOP serializes native ramp counts and stop parameters into the same
ordinary dictionary as scalar overrides. Missing stop parameters are represented
by owned descriptors, never by expanding the artist's Houdini multiparm.
"""

import ast
import math
import posixpath
import re
import hou
from .override_expressions import evaluate as _expression, finite as _finite


class UnsupportedOverride(ValueError):
    def __init__(self, field, reason):
        super().__init__(reason)
        self.field = field


class _MissingChannel(ValueError):
    pass


class _VirtualParameter:
    """A declared but currently uninstantiated native multiparm channel."""

    def __init__(self, node, name, template, index=0):
        self.owner, self.identifier, self.template, self.index = node, name, template, index

    def node(self):
        return self.owner

    def name(self):
        return self.identifier

    def path(self):
        return self.owner.path() + "/" + self.identifier

    def parmTemplate(self):
        return self.template

    def componentIndex(self):
        return self.index

    def parmsReferencingThis(self):
        return ()

    def keyframes(self):
        return ()


class Evaluation:
    def __init__(self, builder, layers):
        self.builder = builder
        self.fields = {}
        self.order = {}
        self.diagnostics = []
        self.omitted = set()
        self._virtual = {}
        self._bundles = {}
        sequence = 0
        for parm in builder.parms():
            template = parm.parmTemplate()
            kind = template.type()
            tags = template.tags()
            if kind == hou.parmTemplateType.Ramp:
                self._bundles[parm.name()] = (parm, 32, "native")
            elif kind == hou.parmTemplateType.Folder and template.folderType() == hou.folderType.MultiparmBlock:
                if tags.get("cycles_rgba_ramp") == "1":
                    self._bundles[parm.name()] = (parm, 32, "rgba")
        for layer in layers:
            try:
                if not isinstance(layer, str) or len(layer.encode("utf-8")) > 65536:
                    raise ValueError("Expected an override dictionary up to 64 KiB")
                tree = ast.parse(layer, mode="eval")
                if not isinstance(tree.body, ast.Dict) or sum(1 for _ in ast.walk(tree)) > 4096:
                    raise ValueError("Expected a bounded dictionary literal")
                if len(tree.body.keys) > 512:
                    raise ValueError("At most 512 override fields are supported per layer")
                if any(not isinstance(key, ast.Constant) or not isinstance(key.value, str) for key in tree.body.keys):
                    raise ValueError("Override keys must be literal parameter names")
            except (SyntaxError, ValueError, RecursionError) as error:
                self.issue("dictionary", str(error))
                continue
            # A broken/executable field must not discard independent valid
            # displacement or a supported lower-priority assignment layer.
            for key, value_node in zip(tree.body.keys, tree.body.values):
                name = key.value
                try:
                    value = ast.literal_eval(value_node)
                    if not name or len(name) > 256 or "\0" in name:
                        raise ValueError("Override names must be nonempty parameter names")
                    parm = self.parameter(builder, name)
                    if parm is not None:
                        parameters = (parm,)
                        components = (value,)
                    else:
                        parameters = self.parameters(builder, name)
                        if parameters is None:
                            raise ValueError("No exported material parameter named " + name)
                        if not isinstance(value, (tuple, list)):
                            raise ValueError("Expected an override tuple for " + name)
                        # Material SOP Color is RGB. An RGBA stop's alpha stays
                        # authored unless a component or vector4 supplies it.
                        rgb = (
                            len(parameters) == 4
                            and parameters[0].parmTemplate().namingScheme() == hou.parmNamingScheme.RGBA
                            and len(value) == 3
                        )
                        if len(value) != len(parameters) and not rgb:
                            raise ValueError("Override tuple size does not match " + name)
                        parameters = parameters[: len(value)]
                        components = value
                    if len(parameters) > 4:
                        raise ValueError("Only scalar, color and vector parameter overrides are supported")
                    owned = {
                        parameter: self._typed(parameter, component)
                        for parameter, component in zip(parameters, components)
                    }
                    self.fields[name] = owned
                    self.order[name] = sequence
                    sequence += 1
                except (ValueError, TypeError, RecursionError) as error:
                    self.issue(name, str(error))
        self._rebuild()

    def _typed(self, parameter, component):
        template = parameter.parmTemplate()
        kind = template.type()
        bundle = self._bundles.get(parameter.name()) if parameter.node() == self.builder else None
        if bundle:
            _finite(component)
            if int(component) != component or not 1 <= component <= bundle[1]:
                raise ValueError("Native ramp count is outside its supported range")
            return int(component)
        if kind == hou.parmTemplateType.String:
            if not isinstance(component, str) or "\0" in component:
                raise ValueError("Expected a string value for " + parameter.name())
        elif kind in (
            hou.parmTemplateType.Float,
            hou.parmTemplateType.Int,
            hou.parmTemplateType.Toggle,
            hou.parmTemplateType.Menu,
        ):
            _finite(component)
            if kind != hou.parmTemplateType.Float and int(component) != component:
                raise ValueError("Expected an integer for " + parameter.name())
            if kind != hou.parmTemplateType.Float and not -2147483648 <= component <= 2147483647:
                raise ValueError("Integer override is outside shader precision for " + parameter.name())
            if kind == hou.parmTemplateType.Toggle and component not in (0, 1):
                raise ValueError("Expected zero or one for " + parameter.name())
            component = float(component) if kind == hou.parmTemplateType.Float else int(component)
        else:
            raise ValueError("This exported parameter needs a separate override adapter: " + parameter.name())
        if hasattr(template, "menuItems") and template.menuItems():
            items = template.menuItems()
            if isinstance(component, str) and component not in items:
                raise ValueError("Unknown menu token for " + parameter.name())
            if isinstance(component, int) and not 0 <= component < len(items):
                raise ValueError("Menu index out of range for " + parameter.name())
        return component

    def _virtual_tuple(self, node, name):
        if node != self.builder:
            return None
        if name in self._virtual:
            return self._virtual[name]
        for prefix, (count, maximum, kind) in self._bundles.items():
            if kind == "native":
                match = re.fullmatch(re.escape(prefix) + r"([1-9][0-9]*)(pos|value|interp|c)", name)
                if not match or not 1 <= int(match[1]) <= maximum:
                    continue
                suffix = match[2]
                color = count.parmTemplate().parmType() == hou.rampParmType.Color
                if suffix == "value" and color or suffix == "c" and not color:
                    continue
                if suffix == "interp":
                    template = hou.MenuParmTemplate(
                        name,
                        "Interpolation",
                        ("constant", "linear", "catmull_rom", "monotone_cubic", "bezier", "bspline", "hermite"),
                    )
                else:
                    template = hou.FloatParmTemplate(
                        name,
                        "Stop",
                        3 if suffix == "c" else 1,
                        naming_scheme=hou.parmNamingScheme.RGBA if suffix == "c" else hou.parmNamingScheme.Base1,
                    )
            else:
                template = None
                for child in count.parmTemplate().parmTemplates():
                    pattern = re.escape(child.name()).replace(r"\#", r"([1-9][0-9]*)")
                    match = re.fullmatch(pattern, name)
                    if match and len(match.groups()) == 1 and 1 <= int(match[1]) <= maximum:
                        template = child
                        break
                if template is None:
                    continue
            size = template.numComponents()
            naming = template.namingScheme() if size > 1 else None
            suffixes = (
                "rgba"
                if naming == hou.parmNamingScheme.RGBA
                else "xyzw"
                if naming == hou.parmNamingScheme.XYZW
                else tuple(str(i + 1) for i in range(size))
            )
            result = tuple(
                _VirtualParameter(node, name + (suffixes[i] if size > 1 else ""), template, i) for i in range(size)
            )
            self._virtual[name] = result
            for parameter in result:
                self._virtual[parameter.name()] = (parameter,)
            return result
        return None

    def parameter(self, node, name):
        actual = node.parm(name)
        if actual is not None:
            return actual
        owned = self._virtual.get(name)
        if owned is not None:
            return owned[0] if len(owned) == 1 else None
        value = self._virtual_tuple(node, name)
        if value is not None:
            return value[0] if len(value) == 1 else None
        for prefix, (_, maximum, kind) in self._bundles.items():
            suffixes = "rgba" if kind == "rgba" else "rgb" if kind == "native" else "xy"
            match = re.fullmatch(re.escape(prefix) + r"([1-9][0-9]*)(c|pos)([" + suffixes + "])", name)
            if match and int(match[1]) <= maximum:
                self._virtual_tuple(node, prefix + match[1] + match[2])
                owned = self._virtual.get(name)
                if owned is not None:
                    return owned[0]
        return None

    def parameters(self, node, name):
        actual = node.parmTuple(name)
        return tuple(actual) if actual is not None else self._virtual_tuple(node, name)

    def issue(self, field, reason):
        issue = dict(
            code="material.override_ignored",
            source=self.builder.path(),
            resource="material_override:" + field,
            attribute="material_override",
            message=field + ": " + reason,
            action="Ignoring this override; retaining supported underlying material values",
        )
        if issue not in self.diagnostics:
            self.diagnostics.append(issue)

    def omit(self, error):
        if error.field in self.omitted:
            raise RuntimeError("Override recovery made no progress")
        self.omitted.add(error.field)
        self.issue(error.field, str(error))
        self._rebuild()

    def _rebuild(self):
        self.values = {
            parm: (value, name)
            for name, values in sorted(self.fields.items(), key=lambda item: self.order[item[0]])
            if name not in self.omitted
            for parm, value in values.items()
        }
        self.deferred = {}
        self.animated_nodes = set()
        self._observe_dependencies()

    def _observe_dependencies(self):
        """Observe registered channel links without evaluating unconsumed nodes."""
        prefix = self.builder.path() + "/"
        for source, (_, name) in self.values.items():
            pending = [source]
            seen = set()
            while pending:
                original = pending.pop()
                if original in seen:
                    continue
                seen.add(original)
                for consumer in original.parmsReferencingThis():
                    if not consumer.path().startswith(prefix) or consumer in self.values:
                        continue
                    self.deferred.setdefault(consumer, {})[name] = (
                        "A contributing channel expression needs an override adapter: " + consumer.path()
                    )
                    pending.append(consumer)

    def reject_if_overridden(self, parameters, reason):
        fields = set()
        for parm in parameters:
            if parm in self.values:
                fields.add(self.values[parm][1])
            fields.update(self.deferred.get(parm, ()))
        if fields:
            raise UnsupportedOverride(max(fields, key=lambda name: self.order[name]), reason)

    def bundle_error(self, count, parameters, error):
        if isinstance(error, UnsupportedOverride):
            raise error
        # An incomplete added stop makes the count change unusable. Existing
        # valid stop overrides and independent shader branches still apply.
        if isinstance(error, _MissingChannel):
            self.reject_if_overridden((count,), str(error))
        self.reject_if_overridden(parameters, str(error))

    def _reference(self, consumer, path):
        target = consumer.node().parm(path)
        if target is not None:
            return target
        absolute = posixpath.normpath(path if path.startswith("/") else consumer.node().path() + "/" + path)
        if posixpath.dirname(absolute) == self.builder.path():
            return self.parameter(self.builder, posixpath.basename(absolute))
        return None

    def _native_read(self, parm, now, active, owner, read):
        # Houdini may register Python channel dependencies only when the active
        # expression is queried. Keep the authored read lazy, then revisit only
        # the existing override-source graph; constants never repeat that walk.
        observe = bool(self.values) and any(key.isExpressionSet() for key in parm.keyframes())

        def observing(operation):
            try:
                return operation()
            finally:
                if observe:
                    self._observe_dependencies()

        if observing(parm.isTimeDependent):
            self.animated_nodes.add(owner)
        if parm in self.deferred:
            return self._read(parm, now, active, owner)
        value = observing(read)
        if parm in self.deferred:
            return self._read(parm, now, active, owner)
        return value

    @staticmethod
    def _expression_result(parm, value):
        template = parm.parmTemplate()
        kind = template.type()
        if kind not in (hou.parmTemplateType.Int, hou.parmTemplateType.Menu, hou.parmTemplateType.Toggle):
            return value
        # Stock HScript rounds integer controls, then clamps ordered menus and
        # toggles. Never reproduce the SDK's overflowed int64 sentinel. A large
        # but safe native integer may clamp to a perfectly valid shader value.
        value = math.floor(_finite(value) + 0.5)
        if not -(1 << 63) <= value < (1 << 63):
            raise ValueError("Contributing integer result is outside safe native precision: " + parm.path())
        items = template.menuItems() if hasattr(template, "menuItems") else ()
        if kind == hou.parmTemplateType.Toggle:
            value = min(max(value, 0), 1)
        elif items:
            value = min(max(value, 0), len(items) - 1)
        else:
            if getattr(template, "minIsStrict", lambda: False)():
                value = max(value, template.minValue())
            if getattr(template, "maxIsStrict", lambda: False)():
                value = min(value, template.maxValue())
        if not -2147483648 <= value <= 2147483647:
            raise ValueError("Contributing integer result is outside shader precision: " + parm.path())
        return int(value)

    def _read(self, parm, now, active, owner):
        if parm in self.values:
            return self.values[parm][0]
        if isinstance(parm, _VirtualParameter):
            raise _MissingChannel("Missing overridden native stop channel: " + parm.path())
        if parm not in self.deferred:
            return self._native_read(parm, now, active, owner, lambda: parm.evalAtTime(now))
        if parm in active or len(active) >= 32:
            raise ValueError("Cyclic or excessively deep promoted channel expression")
        if parm.expressionLanguage() != hou.exprLanguage.Hscript:
            raise ValueError("Contributing Python expressions need a separate override adapter: " + parm.path())
        expression = parm.expression().strip()
        active = active | {parm}

        def channel(kind, path, sample):
            target = self._reference(parm, path)
            if target is None:
                raise ValueError("Missing contributing channel: " + path)
            at = hou.frameToTime(sample) if kind == "chf" else sample if kind == "cht" else now
            return self._read(target, at, active, owner)

        def variable(name):
            self.animated_nodes.add(owner)
            if name == "T":
                return now
            frame = hou.timeToFrame(now)
            return math.floor(frame + 0.5) if name == "F" else frame

        return self._expression_result(parm, _expression(expression, channel, variable))

    def tuple(self, parameter, now, owner=None):
        values = []
        for parm in parameter:
            try:
                values.append(self._read(parm, now, set(), owner or parm.node()))
            except (
                ValueError,
                SyntaxError,
                RecursionError,
                ZeroDivisionError,
                OverflowError,
                hou.OperationFailed,
            ) as error:
                self.reject_if_overridden((parm,), str(error))
                raise
        return tuple(values)

    def parm(self, parameter, now, owner=None):
        return self.tuple((parameter,), now, owner)[0]

    def token(self, parameter, now):
        value = self.parm(parameter, now)
        template = parameter.parmTemplate()
        if isinstance(value, int) and hasattr(template, "menuItems") and template.menuItems():
            return template.menuItems()[value]
        return str(value)

    def count(self, parameter, now, owner=None):
        if (
            parameter.parmTemplate().type() == hou.parmTemplateType.Ramp
            and parameter not in self.values
            and parameter not in self.deferred
        ):
            return self._native_read(
                parameter,
                now,
                set(),
                owner or parameter.node(),
                lambda: parameter.evalAsIntAtFrame(hou.timeToFrame(now)),
            )
        return self.parm(parameter, now, owner)

    def ramp(self, node, now, name="ramp"):
        """Read a promoted native float/RGB ramp from owned count/stop fields."""
        from .ramps import BASES, RampSourceError, _float32

        origin = node.parm(name)
        if origin is None:
            raise RampSourceError(7, "Missing native ramp: " + name)
        count = origin.getReferencedParm()
        template = count.parmTemplate()
        if template.type() != hou.parmTemplateType.Ramp:
            raise RampSourceError(7, "The referenced native ramp is incompatible")
        relevant = [count]
        try:
            number = self.count(count, now, node)
            if (
                not isinstance(number, (int, float))
                or not math.isfinite(number)
                or int(number) != number
                or not 1 <= number <= 32
            ):
                self.reject_if_overridden((count,), "Cycles ramps require 1..32 matching stops")
                raise RampSourceError(1, "Cycles ramps require 1..32 matching stops")
            number = int(number)
            keys = []
            values = []
            bases = []
            color = template.parmType() == hou.rampParmType.Color
            for i in range(1, number + 1):
                prefix = count.name() + str(i)
                position = self.parameter(count.node(), prefix + "pos")
                mode = self.parameter(count.node(), prefix + "interp")
                value = self.parameters(count.node(), prefix + ("c" if color else "value"))
                if position is None or mode is None or value is None:
                    raise RampSourceError(7, "A native ramp stop channel is missing")
                relevant.extend((position, mode, *value))
                keys.append(self.parm(position, now, node))
                basis = self.parm(mode, now, node)
                bases.append(
                    BASES[int(basis)]
                    if isinstance(basis, (int, float)) and int(basis) == basis and 0 <= basis < len(BASES)
                    else "unsupported"
                )
                sample = self.tuple(value, now, node)
                values.append(sample if color else sample[0])
            positions = [_float32(key) for key in keys]
            if any(not 0 <= key <= 1 for key in positions) or any(a >= b for a, b in zip(positions, positions[1:])):
                self.reject_if_overridden(
                    [
                        count,
                        *[self.parameter(count.node(), count.name() + str(i) + "pos") for i in range(1, number + 1)],
                    ],
                    "Native ramp positions must be strictly increasing in [0,1]",
                )
            return dict(bases=bases, keys=keys, values=values, color=color)
        except RampSourceError:
            raise
        except (ValueError, hou.OperationFailed) as error:
            self.bundle_error(count, relevant, error)
            raise RampSourceError(7, "Unusable native ramp channel: " + str(error)) from error
