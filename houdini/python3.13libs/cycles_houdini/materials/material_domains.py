# SPDX-License-Identifier: Apache-2.0
"""Owned surface/displacement stylesheet evaluation without editing host nodes."""

import ast, json
from .material_overrides import Evaluation, UnsupportedOverride


class DomainGraphCapacity(ValueError):
    pass


def merge_graphs(displacement, surface):
    """Keep displacement IDs stable and share exactly equal dependency graphs."""
    from . import MaterialSourceError

    nodes = {}
    links = {}
    versions = {}
    roots = {}
    for role, graph in (("displacement", displacement), ("surface", surface)):
        source = {node["id"]: node for node in graph["nodes"]}
        incoming = {}
        mapped = {}
        active = set()
        for link in graph["links"]:
            incoming.setdefault(link["to"]["node"], []).append(link)

        def visit(key):
            if key in mapped:
                return mapped[key]
            if key in active:
                raise MaterialSourceError("Cyclic scoped material graph")
            if key not in source:
                raise RuntimeError("Scoped graph has an unowned shader endpoint")
            active.add(key)
            node = source[key]
            edges = []
            for link in incoming.get(key, ()):
                edges.append((link["to"]["socket"], visit(link["from"]["node"]), link["from"]["socket"]))
            signature = (
                node["definition"],
                json.dumps(node.get("parameters", {}), sort_keys=True, separators=(",", ":")),
                tuple(sorted(edges)),
            )
            choices = versions.setdefault(key, {})
            if signature in choices:
                target = choices[signature]
            else:
                target = key if key not in nodes else key + "/stylesheet:" + role
                if target in nodes:
                    raise RuntimeError("Scoped shader identity collision")
                choices[signature] = target
                nodes[target] = dict(node, id=target)
                for socket, origin, output in edges:
                    identity = (origin, output, target, socket)
                    links[identity] = {"from": dict(node=origin, socket=output), "to": dict(node=target, socket=socket)}
            active.remove(key)
            mapped[key] = target
            return target

        for key in source:
            visit(key)
        for name in ("surface", "volume", "displacement"):
            if name in graph:
                roots[name] = dict(graph[name], node=mapped[graph[name]["node"]])
    if len(nodes) > 256 or len(links) > 2048:
        raise DomainGraphCapacity("Scoped shader branches exceed the 256-node/2048-link material budget")
    if not any(name in roots for name in ("surface", "volume")):
        raise MaterialSourceError("Scoped material needs surface or volume shading")
    extra = {
        key: value
        for graph in (surface, displacement)
        for key, value in graph.items()
        if key not in ("schema_version", "nodes", "links", "surface", "volume", "displacement")
    }
    return dict(
        schema_version=1,
        nodes=sorted(nodes.values(), key=lambda value: value["id"]),
        links=[links[key] for key in sorted(links)],
        **roots,
        **extra,
    )


class ScopedEvaluation:
    def __init__(self, builder, layers, surface_layers, displacement_layers):
        self.material = Evaluation(builder, layers)
        self.surface = Evaluation(builder, [*layers, *surface_layers]) if surface_layers else self.material
        self.displacement = (
            Evaluation(builder, [*layers, *displacement_layers]) if displacement_layers else self.material
        )
        self.builder = builder
        self.layers = list(layers)
        self.surface_layers = list(surface_layers)
        self.displacement_layers = list(displacement_layers)
        self.pending_capacity = None
        self.capacity_diagnostics = []
        self.current = self.material

    def graph(self, builder, now):
        from . import material_graph

        self.current = self.displacement
        displacement = material_graph(builder, now, evaluation=self.displacement, output_scope="displacement")
        self.current = self.surface
        surface = material_graph(builder, now, evaluation=self.surface, output_scope="surface")
        try:
            return merge_graphs(displacement, surface)
        except DomainGraphCapacity as error:
            # Drop an effective scoped field and retry, preserving generic
            # material controls and the authored graph when a split is too big.
            for role, evaluation, layers in (
                ("surface", self.surface, self.surface_layers),
                ("displacement", self.displacement, self.displacement_layers),
            ):
                for layer in reversed(layers):
                    try:
                        values = ast.literal_eval(layer)
                    except (ValueError, SyntaxError, RecursionError):
                        continue
                    if not isinstance(values, dict):
                        continue
                    for name in reversed(values):
                        if name in evaluation.fields and name not in evaluation.omitted:
                            self.current = evaluation
                            self.pending_capacity = (role, name)
                            raise UnsupportedOverride(name, str(error)) from error
            from . import MaterialSourceError

            raise MaterialSourceError(str(error)) from error

    def omit(self, error):
        if self.pending_capacity is None:
            self.current.omit(error)
            return
        role, name = self.pending_capacity
        self.pending_capacity = None
        old = getattr(self, role)
        old.issue(name, str(error))
        self.capacity_diagnostics.extend(
            (role, issue) for issue in old.diagnostics if issue not in self.material.diagnostics
        )
        # Capacity recovery removes only this role's field. Retain a valid
        # generic material value with the same name instead of omitting both.
        filtered = []
        for layer in getattr(self, role + "_layers"):
            try:
                values = ast.literal_eval(layer)
            except (ValueError, SyntaxError, RecursionError):
                filtered.append(layer)
                continue
            if isinstance(values, dict):
                values.pop(name, None)
                filtered.append(repr(values))
            else:
                filtered.append(layer)
        setattr(self, role + "_layers", filtered)
        replacement = Evaluation(self.builder, [*self.layers, *filtered])
        replacement.omitted.update(old.omitted)
        replacement._rebuild()
        setattr(self, role, replacement)

    def parm(self, parameter, now):
        self.current = self.material
        return self.material.parm(parameter, now)

    @property
    def diagnostics(self):
        result = list(self.material.diagnostics)
        for role, category, evaluation in (
            ("surface", "surfaceParameters", self.surface),
            ("displacement", "displaceParameters", self.displacement),
        ):
            for issue in [
                *evaluation.diagnostics,
                *(issue for domain, issue in self.capacity_diagnostics if domain == role),
            ]:
                if issue in self.material.diagnostics:
                    continue
                owned = dict(
                    issue,
                    attribute="material_stylesheet",
                    resource=category + ":" + issue["resource"],
                    message=category + "." + issue["message"],
                )
                if owned not in result:
                    result.append(owned)
        return result
