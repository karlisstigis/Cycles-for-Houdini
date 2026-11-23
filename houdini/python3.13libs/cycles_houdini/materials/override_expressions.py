# SPDX-License-Identifier: Apache-2.0
"""Bounded numeric HScript substitution for contributing owned overrides.

This is an allowlist, not script execution. Channel lookup and time conversion
remain with the host evaluator; this module owns only immutable syntax/results.
"""

import ast
from functools import lru_cache
import math
import re


_TOKENS = re.compile(r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\$[A-Za-z_][A-Za-z_0-9]*""")
_ARITY = {"abs": 1, "min": 2, "max": 2, "clamp": 3, "fit": 5, "efit": 5, "fit01": 3, "floor": 1, "ceil": 1, "int": 1}


def finite(value):
    if not isinstance(value, (int, float)):
        raise ValueError("Expected a finite numeric value")
    try:
        valid = math.isfinite(value) and abs(value) <= 3.402823466e38
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError("Numeric value is outside finite shader precision")
    return value


@lru_cache(maxsize=256)
def _syntax(expression):
    if len(expression.encode("utf-8")) > 4096:
        raise ValueError("Contributing channel expression exceeds 4 KiB")
    # Skip quoted literals. A prefix absent from the source prevents an authored
    # identifier from impersonating the internal representation of a $ variable.
    prefix = "_cycles_time_"
    while prefix in expression:
        prefix += "x"
    variables = {}

    def token(match):
        text = match.group()
        if not text.startswith("$"):
            return text
        if text not in ("$F", "$FF", "$T"):
            raise ValueError("Unsupported contributing time variable: " + text)
        name = prefix + text[1:]
        variables[name] = text[1:]
        return name

    tree = ast.parse(_TOKENS.sub(token, expression), mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 256:
        raise ValueError("Contributing channel expression exceeds 256 syntax nodes")
    return tree, variables


def _remap(value, old_min, old_max, new_min, new_max, clamped):
    # Exact stock HScript boundaries, including reversed and zero-width ranges.
    if old_min == old_max:
        factor = 0.5 if not clamped or value == old_min else 0.0 if value < old_min else 1.0
    else:
        factor = (value - old_min) / (old_max - old_min)
    if clamped:
        factor = min(max(factor, 0.0), 1.0)
    return new_min + factor * (new_max - new_min)


def _function(name, args):
    if name == "abs":
        return abs(args[0])
    if name == "min":
        return min(args)
    if name == "max":
        return max(args)
    if name == "floor":
        return math.floor(args[0])
    if name == "ceil":
        return math.ceil(args[0])
    if name == "int":
        return int(args[0])
    if name == "clamp":
        return min(max(args[0], min(args[1:])), max(args[1:]))
    if name == "fit01":
        return _remap(args[0], 0.0, 1.0, args[1], args[2], True)
    return _remap(*args, name == "fit")


def evaluate(expression, channel, variable):
    """Evaluate the approved syntax using owned channel/time callbacks only."""
    tree, variables = _syntax(expression)

    def numeric(node, depth=0):
        if depth >= 32:
            raise ValueError("Contributing channel expression is too deep")
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return finite(node.value)
        if isinstance(node, ast.Name) and node.id in variables:
            return finite(variable(variables[node.id]))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = numeric(node.operand, depth + 1)
            return finite(value if isinstance(node.op, ast.UAdd) else -value)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            left = numeric(node.left, depth + 1)
            right = numeric(node.right, depth + 1)
            return finite(
                left + right
                if isinstance(node.op, ast.Add)
                else left - right
                if isinstance(node.op, ast.Sub)
                else left * right
                if isinstance(node.op, ast.Mult)
                else left / right
            )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords:
            name = node.func.id
            if name in ("ch", "chs", "chf", "cht"):
                arity = 2 if name in ("chf", "cht") else 1
                if (
                    len(node.args) != arity
                    or not isinstance(node.args[0], ast.Constant)
                    or not isinstance(node.args[0].value, str)
                ):
                    raise ValueError("Channel references require a literal path and the documented argument count")
                path = node.args[0].value
                if not path or len(path) > 4096 or any(c in path for c in ("\0", "$", "`")):
                    raise ValueError("Only nonempty literal channel paths are supported")
                if name == "chs" and tree.body is not node:
                    raise ValueError("Only direct promoted string references are supported")
                sample = numeric(node.args[1], depth + 1) if arity == 2 else None
                value = channel(name, path, sample)
                if name == "chs":
                    if not isinstance(value, str):
                        raise ValueError("Only direct promoted string references are supported")
                    return value
                return finite(value)
            if name in _ARITY:
                if len(node.args) != _ARITY[name]:
                    raise ValueError(name + " requires " + str(_ARITY[name]) + " arguments")
                return finite(_function(name, [numeric(arg, depth + 1) for arg in node.args]))
        raise ValueError(
            "Unsupported contributing expression; use literal channels, approved numeric functions, $F/$FF/$T and + - * / arithmetic"
        )

    return numeric(tree.body)
