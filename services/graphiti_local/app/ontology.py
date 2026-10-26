"""Compile persisted ontology JSON into Graphiti models without executing code."""
import keyword
from collections import defaultdict
from datetime import datetime

from pydantic import Field, create_model

RESERVED = {"uuid", "uuid_", "name", "group_id", "graph_id", "labels", "summary",
            "created_at", "attributes", "name_embedding", "fact", "fact_embedding",
            "source_node_uuid", "target_node_uuid", "episodes", "valid_at", "invalid_at",
            "expired_at", "model_config", "model_fields"}
INTERNAL_LABELS = {"Entity", "Episodic", "Community", "Saga"}
TYPES = {"text": str, "string": str, "str": str, "integer": int, "int": int,
         "number": float, "float": float, "boolean": bool, "bool": bool,
         "datetime": datetime, "date": str, "list": list[str]}


def identifier(value):
    if not isinstance(value, str) or not value.isidentifier() or keyword.iskeyword(value) or value.startswith("_"):
        raise ValueError(f"Invalid ontology identifier: {value!r}")
    return value


def compile_ontology(ontology):
    entities, edges = {}, {}
    for kind, target in [("entity_types", entities), ("edge_types", edges)]:
        for spec in ontology.get(kind, []):
            name = identifier(spec["name"])
            if name in target or name in INTERNAL_LABELS:
                raise ValueError(f"Duplicate or reserved ontology type: {name}")
            fields = {}
            for attr in spec.get("attributes", []):
                if isinstance(attr, str):
                    attr = {"name": attr, "type": "text", "description": attr}
                field = identifier(attr["name"])
                if field in RESERVED or field.startswith("model_") or field in fields:
                    raise ValueError(f"Duplicate or reserved ontology attribute: {field}")
                dtype = TYPES.get(attr.get("type", "text").lower())
                if dtype is None:
                    raise ValueError(f"Unsupported attribute type: {attr.get('type')}")
                fields[field] = (dtype | None, Field(default=None, description=attr.get("description", field)))
            target[name] = create_model(name, __doc__=spec.get("description", name), **fields)
    mapping = defaultdict(list)
    for spec in ontology.get("edge_types", []):
        for pair in spec.get("source_targets", []):
            source, target = pair["source"], pair["target"]
            if source not in entities or target not in entities:
                raise ValueError(f"Unknown ontology endpoint: {source} -> {target}")
            if spec["name"] not in mapping[(source, target)]:
                mapping[(source, target)].append(spec["name"])
    return entities, edges, dict(mapping)
