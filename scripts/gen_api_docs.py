"""Generate the per-group API reference (reference.md + schemas.md) from Lumen's OpenAPI document.

The OpenAPI document is the source of truth; nothing here is hand-written. Usage:

    uv run python scripts/gen_api_docs.py --url http://127.0.0.1:8012/openapi.json --out docs/api
    uv run python scripts/gen_api_docs.py --openapi /path/to/openapi.json --out docs/api

Every operation must belong to a group in GROUPS; an unmapped tag fails the run so that
new routes cannot silently disappear from the documentation.
"""

from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys
import urllib.request

METHODS = ("get", "post", "put", "patch", "delete")

# tag -> (group directory, reference file)
GROUPS: dict[str, tuple[str, str]] = {
    "Chat API Keys": ("auth", "reference.md"),
    "Policy": ("auth", "reference.md"),
    "Discovery": ("auth", "reference.md"),
    "Health": ("auth", "reference.md"),
    "AI Compat Discovery": ("compat", "reference.md"),
    "Chat Completions": ("chat", "reference.md"),
    "Chat Conversations": ("chat", "reference.md"),
    "Agent Runtime": ("chat", "reference.md"),
    "Chat Agents": ("agents-workspaces", "reference.md"),
    "Chat Memory": ("agents-workspaces", "reference.md"),
    "Chat Workspaces": ("agents-workspaces", "reference.md"),
    "Chat Code Workspaces": ("agents-workspaces", "reference.md"),
    "Chat Assets": ("agents-workspaces", "reference.md"),
    "Images": ("media", "reference.md"),
    "Audio": ("media", "reference.md"),
    "Realtime Voice": ("media", "reference.md"),
    "OpenAI Images Compat": ("media", "reference.md"),
    "OpenAI Audio Compat": ("media", "reference.md"),
    "OpenAI Compat": ("compat", "reference.md"),
    "OpenAI Responses Compat": ("compat", "reference.md"),
    "Anthropic Compat": ("compat", "reference.md"),
    "OpenAI Files Compat": ("compat", "reference.md"),
    "OpenAI Batch Compat": ("compat", "reference.md"),
    "Batches": ("compat", "reference.md"),
    "Coding CLI Model Catalog": ("compat", "reference.md"),
    "Chat Extensions": ("tools-mcp", "reference.md"),
    "Plugin Catalogue": ("tools-mcp", "reference.md"),
    "Chat MCP OAuth": ("tools-mcp", "reference.md"),
    "Chat Extensions Admin": ("tools-mcp", "reference-admin.md"),
    "Plugin Administration": ("tools-mcp", "reference-admin.md"),
    "Claude Gateway": ("integrations", "reference.md"),
    "Claude Gateway Authorization": ("integrations", "reference.md"),
    "Chat Usage": ("billing", "reference.md"),
    "Chat Stats": ("billing", "reference.md"),
    "Chat Quotas": ("billing", "reference.md"),
    "Chat Administration": ("admin", "reference.md"),
    "Agent Runtime Administration": ("admin", "reference.md"),
}

AUTH_LABELS = {
    "APIKeyBearer": "API key (Bearer)",
    "XApiKey": "API key (x-api-key)",
    "KeystoneToken": "Keystone (X-Auth-Token)",
    "KeystoneBearer": "Keystone (Bearer)",
}


def load_spec(args: argparse.Namespace) -> dict:
    if args.openapi:
        return json.loads(pathlib.Path(args.openapi).read_text())
    with urllib.request.urlopen(args.url, timeout=30) as response:  # noqa: S310 - operator-supplied URL
        return json.loads(response.read())


def ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def operations(spec: dict):
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method in METHODS:
                yield path, method, op


def group_of(op: dict) -> tuple[str, str]:
    tag = (op.get("tags") or ["(none)"])[0]
    if tag not in GROUPS:
        raise SystemExit(f"unmapped tag {tag!r}; add it to GROUPS in scripts/gen_api_docs.py")
    return GROUPS[tag]


def referenced_schemas(spec: dict, schema: object, seen: set[str]) -> None:
    """Collect the transitive closure of component schemas referenced from a schema node."""
    if isinstance(schema, dict):
        if "$ref" in schema:
            name = ref_name(schema["$ref"])
            if name not in seen:
                seen.add(name)
                referenced_schemas(spec, spec["components"]["schemas"].get(name, {}), seen)
            return
        for value in schema.values():
            referenced_schemas(spec, value, seen)
    elif isinstance(schema, list):
        for value in schema:
            referenced_schemas(spec, value, seen)


def type_text(spec: dict, node: dict) -> str:
    if not isinstance(node, dict) or not node:
        return "any"
    if "$ref" in node:
        return f"[{ref_name(node['$ref'])}](#{ref_name(node['$ref']).lower()})"
    for key in ("anyOf", "oneOf"):
        if key in node:
            parts = [type_text(spec, option) for option in node[key] if option.get("type") != "null"]
            nullable = any(option.get("type") == "null" for option in node[key])
            text = " \\| ".join(parts) or "any"
            return f"{text} \\| null" if nullable else text
    if "allOf" in node and len(node["allOf"]) == 1:
        return type_text(spec, node["allOf"][0])
    kind = node.get("type")
    if kind == "array":
        return f"array of {type_text(spec, node.get('items', {}))}"
    if kind == "object" and "additionalProperties" in node and "properties" not in node:
        return f"map of {type_text(spec, node['additionalProperties']) if isinstance(node['additionalProperties'], dict) else 'any'}"
    if "enum" in node:
        return "enum: " + ", ".join(f"`{value}`" for value in node["enum"])
    if "const" in node:
        return f"const `{node['const']}`"
    if kind:
        return kind if not node.get("format") else f"{kind} ({node['format']})"
    return "any"


def schema_section(spec: dict, name: str) -> list[str]:
    schema = spec["components"]["schemas"].get(name, {})
    lines = [f"### {name}", ""]
    if schema.get("description"):
        lines += [schema["description"].strip(), ""]
    if "enum" in schema:
        lines += ["Enum values: " + ", ".join(f"`{value}`" for value in schema["enum"]), ""]
        return lines
    props = schema.get("properties")
    if not props:
        lines += [f"Type: {type_text(spec, schema)}", ""]
        return lines
    required = set(schema.get("required", []))
    lines += ["| Field | Type | Required | Default | Notes |", "| --- | --- | --- | --- | --- |"]
    for field, node in props.items():
        default = node.get("default", "—")
        default_text = f"`{json_text(default)}`" if default != "—" else "—"
        notes = (node.get("description") or node.get("title") or "").replace("|", "\\|").replace("\n", " ")
        if node.get("maxItems") is not None:
            notes = (notes + f" (max {node['maxItems']} items)").strip()
        if node.get("maxLength") is not None:
            notes = (notes + f" (max {node['maxLength']} chars)").strip()
        if node.get("minimum") is not None or node.get("exclusiveMinimum") is not None:
            notes = (notes + " (numeric lower bound)").strip()
        lines.append(
            f"| `{field}` | {type_text(spec, node)} | {'yes' if field in required else 'no'} | {default_text} | {notes} |"
        )
    lines.append("")
    return lines


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def build(spec: dict, out: pathlib.Path) -> dict[str, int]:
    rows: dict[tuple[str, str], list] = collections.defaultdict(list)
    schema_names: dict[str, set[str]] = collections.defaultdict(set)  # per group directory
    counts: dict[str, int] = {}
    for path, method, op in operations(spec):
        key = group_of(op)
        sec = op.get("security")
        if sec is None:
            sec = spec.get("security", [])
        auth = ", ".join(AUTH_LABELS.get(k, k) for entry in sec for k in entry) or "none (public)"
        scopes = op.get("x-required-api-key-scopes") or []
        cond = op.get("x-conditional-api-key-scopes") or {}
        scope_text = ", ".join(f"`{s}`" for s in scopes) or "—"
        if cond:
            clauses = "; ".join(
                f"{condition}: " + ", ".join(f"`{s}`" for s in names) for condition, names in cond.items()
            )
            scope_text += f" (conditional: {clauses})"
        body = op.get("requestBody", {}).get("content", {})
        if body:
            ctype, spec_body = next(iter(body.items()))
            ref = spec_body.get("schema", {}).get("$ref", "")
            body_text = f"[{ref_name(ref)}](schemas.md#{ref_name(ref).lower()})" if ref else ctype
        else:
            body_text = "—"
        ok = next((code for code in op.get("responses", {}) if code.startswith("2")), "—")
        rows[key].append((path, method.upper(), op.get("summary", ""), auth, scope_text, body_text, ok, op.get("operationId", "")))
        seen: set[str] = set()
        referenced_schemas(spec, op.get("requestBody", {}), seen)
        referenced_schemas(spec, op.get("responses", {}), seen)
        referenced_schemas(spec, op.get("parameters", []), seen)
        schema_names[key[0]] |= seen

    for (dirname, fname), items in rows.items():
        target = out / dirname
        target.mkdir(parents=True, exist_ok=True)
        lines = [
            f"# Endpoint reference: {dirname} ({fname})",
            "",
            "> Generated by `scripts/gen_api_docs.py` from the live OpenAPI document (`GET /openapi.json`). Do not edit by hand; regenerate after route changes.",
            "",
            "| Method | Path | Summary | Auth | Scopes | Request body | Success | operationId |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for path, method, summary, auth, scopes, body, ok, opid in sorted(items, key=lambda r: (r[0], r[1])):
            lines.append(f"| {method} | `{path}` | {summary} | {auth} | {scopes} | {body} | {ok} | `{opid}` |")
        lines.append("")
        (target / fname).write_text("\n".join(lines))
        counts[f"{dirname}/{fname}"] = len(items)

    for dirname, names in schema_names.items():
        target = out / dirname
        lines = [
            f"# Schemas: {dirname}",
            "",
            "> Generated by `scripts/gen_api_docs.py` from the live OpenAPI document. Do not edit by hand.",
            "",
            "Each section lists the fields of one component schema referenced by this group's operations, with type, required flag, default and the description from the OpenAPI model. Nested models link to their own section.",
            "",
        ]
        for name in sorted(names):
            if name in spec["components"]["schemas"]:
                lines += schema_section(spec, name)
        (target / "schemas.md").write_text("\n".join(lines))
        counts[f"{dirname}/schemas.md"] = len([n for n in names if n in spec["components"]["schemas"]])
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="OpenAPI JSON URL, e.g. http://127.0.0.1:8012/openapi.json")
    source.add_argument("--openapi", help="path to a saved OpenAPI JSON document")
    parser.add_argument("--out", required=True, help="output directory, e.g. docs/api")
    args = parser.parse_args()
    spec = load_spec(args)
    counts = build(spec, pathlib.Path(args.out))
    total_ops = sum(v for k, v in counts.items() if k.endswith(("reference.md", "reference-admin.md")))
    for name in sorted(counts):
        print(f"{name}: {counts[name]}")
    print(f"operations: {total_ops}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
