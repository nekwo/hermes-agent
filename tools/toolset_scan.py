"""Read literal registration tables without importing or executing tool code."""
from __future__ import annotations

import ast

_UNKNOWN = object()


def registration_rows(tree: ast.Module):
    """Yield direct calls and calls in finite, literal module-level tables.

    Unknown values stay unknown: handler expressions are never evaluated. A loop
    whose table cannot be read yields its calls with unresolved arguments, so the
    manifest generator refuses a truncated inventory rather than losing tools.
    """
    definitions = {}
    reassigned = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign):
            targets, value = [stmt.target], stmt.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                if target.id in definitions:
                    reassigned.add(target.id)
                definitions[target.id] = value

    def value(node, bindings, visiting=frozenset()):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in bindings:
                return bindings[node.id]
            if node.id in reassigned or node.id in visiting:
                return _UNKNOWN
            target = definitions.get(node.id)
            return value(target, bindings, visiting | {node.id}) if target else _UNKNOWN
        if isinstance(node, (ast.Tuple, ast.List)):
            return [value(item, bindings, visiting) for item in node.elts]
        if isinstance(node, ast.Dict):
            result = {}
            for key, item in zip(node.keys, node.values):
                key_value = value(key, bindings, visiting)
                if isinstance(key_value, str):
                    result[key_value] = value(item, bindings, visiting)
            return result
        if isinstance(node, ast.Subscript):
            owner = value(node.value, bindings, visiting)
            key = value(node.slice, bindings, visiting)
            if isinstance(owner, dict) and isinstance(key, str):
                return owner.get(key, _UNKNOWN)
            if isinstance(owner, list) and isinstance(key, int) and -len(owner) <= key < len(owner):
                return owner[key]
        return _UNKNOWN

    def bind(target, item, bindings):
        if isinstance(target, ast.Name):
            bindings[target.id] = item
        elif isinstance(target, (ast.Tuple, ast.List)):
            items = item if isinstance(item, list) else []
            star = next((i for i, node in enumerate(target.elts) if isinstance(node, ast.Starred)), None)
            for i, node in enumerate(target.elts):
                if isinstance(node, ast.Starred):
                    tail = len(target.elts) - i - 1
                    bind(node.value, items[i:len(items) - tail if tail else None], bindings)
                else:
                    index = i if star is None or i < star else i - len(target.elts)
                    selected = items[index] if -len(items) <= index < len(items) else _UNKNOWN
                    bind(node, selected, bindings)

    def walk(statements, bindings):
        for stmt in statements:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                call = stmt.value
                if (isinstance(call.func, ast.Attribute) and call.func.attr == "register"
                        and isinstance(call.func.value, ast.Name) and call.func.value.id == "registry"):
                    kw = {item.arg: item.value for item in call.keywords if item.arg}
                    name = kw.get("name") or (call.args[0] if call.args else None)
                    toolset = kw.get("toolset") or (call.args[1] if len(call.args) > 1 else None)
                    yield stmt, value(name, bindings), value(toolset, bindings)
            elif isinstance(stmt, ast.For):
                rows = value(stmt.iter, bindings)
                # Bound the reader even for an accidentally huge literal table.
                if not isinstance(rows, list) or len(rows) > 10000:
                    rows = [_UNKNOWN]
                for row in rows:
                    local = dict(bindings)
                    bind(stmt.target, row, local)
                    yield from walk(stmt.body, local)
                yield from walk(stmt.orelse, bindings)

    yield from walk(tree.body, {})
