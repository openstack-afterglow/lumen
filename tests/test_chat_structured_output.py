import pytest

from lumen.services.structured_output import StructuredOutputError, parse_structured_output


def test_json_object_requires_object_and_returns_canonical_part():
    part = parse_structured_output('{"answer":"ok"}', {"kind": "json_object"})
    assert part == {
        "type": "structured",
        "schema_name": "json_object",
        "schema_version": "1",
        "value": {"answer": "ok"},
        "valid": True,
    }
    with pytest.raises(StructuredOutputError, match="must be an object"):
        parse_structured_output('["not-object"]', {"kind": "json_object"})


def test_json_schema_validates_provider_output_without_exposing_raw_failure():
    response_format = {
        "kind": "json_schema",
        "name": "answer",
        "version": "1",
        "schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        },
    }
    assert parse_structured_output('{"answer":"ok"}', response_format)["value"] == {"answer": "ok"}
    with pytest.raises(StructuredOutputError, match="does not match schema"):
        parse_structured_output('{"answer": 1}', response_format)
