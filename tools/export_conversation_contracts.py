"""Generate frontend types and validators from the same models used by OpenAPI."""

import argparse
import json
from pathlib import Path

from ebiz_deployment.supply_chain_bff import conversation_contracts as models

NAMES = ["ConversationCreated", "TurnAccepted", "TurnSnapshot", "ConversationSnapshot", "TurnEvent"]


def ts(schema):
    if isinstance(schema, bool):
        return "unknown" if schema else "never"
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[-1]
    if "anyOf" in schema:
        return " | ".join(ts(s) for s in schema["anyOf"])
    if "enum" in schema:
        return " | ".join(json.dumps(s) for s in schema["enum"])
    kind = schema.get("type")
    if kind == "array":
        return f"Array<{ts(schema['items'])}>"
    if kind == "object":
        if "properties" not in schema:
            return f"Record<string, {ts(schema.get('additionalProperties', {}))}>"
        return (
            "{\n"
            + "\n".join(
                f"  {key}{'' if key in schema.get('required', []) else '?'}: {ts(value)}"
                for key, value in schema["properties"].items()
            )
            + "\n}"
        )
    return {
        "string": "string",
        "integer": "number",
        "number": "number",
        "boolean": "boolean",
        "null": "null",
    }.get(kind, "unknown")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    schemas = {name: getattr(models, name).model_json_schema() for name in NAMES}
    definitions = {}
    for value in schemas.values():
        definitions.update(value.get("$defs", {}))
    definitions.update(schemas)
    content = "// Generated from BFF conversation_contracts.py; do not hand-edit.\n"
    content += "\n".join(
        f"export type {name} = {ts(schema)}\n" for name, schema in definitions.items()
    )
    content += (
        "export const conversationSchemas = "
        + json.dumps(schemas, ensure_ascii=False, indent=2)
        + " as const\n"
    )
    args.output.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
