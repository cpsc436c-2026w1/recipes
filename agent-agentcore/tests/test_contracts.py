"""The tool contracts: the pydantic models are the schema the Gateway gets, the check before a call, and the shape
of every result."""
import pytest

from main.contracts import (SCHEMA_KEYS, TOOLS, WRITE_TOOLS, BookDetail, Holds, HoldResult, SearchResult, ToolError,
                            check_arguments, parse_result, short_name, tool_schema)
from tests.fakes import MODEL, check_patterns
from botocore.validate import ParamValidator


def test_the_gateway_schema_has_one_definition_per_tool_with_the_models_fields():
    schema = {t["name"]: t for t in tool_schema()}
    assert set(schema) == set(TOOLS) and WRITE_TOOLS <= set(TOOLS)
    for name, (description, arguments, _) in TOOLS.items():
        assert schema[name]["description"] == description
        assert set(schema[name]["inputSchema"]["properties"]) == set(arguments.model_fields)
        required = {f for f, info in arguments.model_fields.items() if info.is_required()}
        assert set(schema[name]["inputSchema"].get("required", [])) == required


def test_the_gateway_schema_uses_only_the_keys_the_service_takes():
    def keys(node):
        found = set(node) - {"properties"}
        for sub in node.get("properties", {}).values():
            found |= keys(sub)
        return found | ({"properties"} if "properties" in node else set())
    for tool in tool_schema():
        assert keys(tool["inputSchema"]) <= set(SCHEMA_KEYS), tool["name"]


def test_the_gateway_schema_passes_the_services_own_parameter_check():
    shape = MODEL.operation_model("CreateGatewayTarget").input_shape
    params = {"gatewayIdentifier": "test-agent-gateway-0000000001", "name": "library-tools",
              "targetConfiguration": {"mcp": {"lambda": {
                  "lambdaArn": "arn:aws:lambda:ca-central-1:123456789012:function:test-agent-tools",
                  "toolSchema": {"inlinePayload": tool_schema()}}}}}
    report = ParamValidator().validate(params, shape)
    assert not report.has_errors(), report.generate_report()
    assert check_patterns(params, shape) == []


@pytest.mark.parametrize("name, arguments", [
    ("search_books", {"query": "austen"}),
    ("search_books", {"query": "novel", "filters": {"available_only": True, "max_pages": 300}}),
    ("library-tools___get_book", {"book_id": "b01"}),
    ("get_holds", {"member": "sam"}),
    ("place_hold", {"member": "sam", "book_id": "b01"}),
])
def test_arguments_that_match_the_model_pass(name, arguments):
    checked, problem = check_arguments(name, arguments)
    assert problem == "" and checked.items() >= {k: v for k, v in arguments.items() if k != "filters"}.items()


@pytest.mark.parametrize("name, arguments, says", [
    ("search_books", {"q": "austen"}, "query: Field required"),
    ("search_books", {"query": "austen", "limit": 500}, "limit: Extra inputs are not permitted"),
    ("search_books", {"query": "novel", "filters": {"max_pages": -1}}, "filters.max_pages"),
    ("get_book", {"book_id": ""}, "book_id"),
    ("place_hold", {"member": "sam"}, "book_id: Field required"),
    ("drop_table", {}, "unknown tool drop_table"),
])
def test_arguments_that_do_not_match_are_refused_with_the_reason(name, arguments, says):
    checked, problem = check_arguments(name, arguments)
    assert checked == {} and says in problem


def test_every_result_of_the_tools_has_its_models_shape(library):
    assert isinstance(parse_result("search_books", library.search_books("austen")), SearchResult)
    assert isinstance(parse_result("get_book", library.get_book("b01")), BookDetail)
    assert isinstance(parse_result("get_holds", library.get_holds("sam")), Holds)
    assert isinstance(parse_result("library-tools___place_hold", library.place_hold("sam", "b01", "k1")), HoldResult)
    assert isinstance(parse_result("get_book", library.get_book("nope")), ToolError)


def test_a_result_with_another_shape_is_an_error():
    with pytest.raises(ValueError):
        parse_result("get_book", {"title": "Dune"})


def test_the_local_tools_give_the_llm_the_schema_the_gateway_gets(library):
    from main.agent import local_tools

    local = {t.tool_name: t.tool_spec for t in local_tools(library)}
    for definition in tool_schema():
        spec = local[definition["name"]]
        assert spec["description"] == definition["description"]
        assert spec["inputSchema"]["json"] == definition["inputSchema"]


def test_short_name_drops_the_gateways_target():
    assert short_name("library-tools___place_hold") == short_name("place_hold") == "place_hold"
