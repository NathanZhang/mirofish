import pytest
from app.ontology import compile_ontology


def ontology():
    return {"entity_types": [{"name": "Person", "attributes": [
        {"name": "age", "type": "integer", "description": "年龄"}]}],
        "edge_types": [{"name": "KNOWS", "source_targets": [{"source": "Person", "target": "Person"}]}]}


def test_typed_attributes_and_relationship_endpoints():
    entities, edges, mapping = compile_ontology(ontology())
    assert entities["Person"](age="42").age == 42
    assert mapping == {("Person", "Person"): ["KNOWS"]}


@pytest.mark.parametrize("field", ["uuid", "fact", "source_node_uuid", "model_config", "__class__", "x);evil()"])
def test_reserved_or_executable_attribute_rejected(field):
    value = ontology()
    value["entity_types"][0]["attributes"][0]["name"] = field
    with pytest.raises(ValueError):
        compile_ontology(value)


def test_unknown_endpoint_and_duplicate_types_rejected():
    value = ontology()
    value["edge_types"][0]["source_targets"][0]["target"] = "Missing"
    with pytest.raises(ValueError, match="Unknown"):
        compile_ontology(value)
    value = ontology()
    value["entity_types"] *= 2
    with pytest.raises(ValueError, match="Duplicate"):
        compile_ontology(value)
