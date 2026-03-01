"""Tests for scripts/parse_models.py — botocore model parser.

Tests use two approaches:
  1. Embedded minimal model dicts — no network required, fully deterministic.
  2. Live botocore data (STS GetCallerIdentity) — verifies against real models.

All tests follow the project coding standards:
  - No silent fallbacks: every error path is asserted explicitly.
  - No magic numbers: constants are named.
"""

import json
import os
import sys
import pytest

# ---------------------------------------------------------------------------
# Path setup — make scripts/ importable
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from parse_models import (
    BotocoreModelParser,
    _botocore_scalar_to_pie,
    _collect_reachable_shapes,
    _resolve_pie_type,
    generate_json,
    generate_pie,
    load_model_by_service_name,
    load_model_from_file,
)


# ---------------------------------------------------------------------------
# Minimal embedded models — no botocore I/O needed
# ---------------------------------------------------------------------------

MINIMAL_MODEL = {
    "metadata": {
        "endpointPrefix": "myservice",
        "protocol": "rest-json",
        "apiVersion": "2024-01-01",
        "signatureVersion": "v4",
        "serviceId": "MyService",
    },
    "operations": {
        "Echo": {
            "name": "Echo",
            "http": {"method": "POST", "requestUri": "/echo"},
            "input": {"shape": "EchoRequest"},
            "output": {"shape": "EchoResponse"},
        }
    },
    "shapes": {
        "EchoRequest": {
            "type": "structure",
            "required": ["Message"],
            "members": {
                "Message": {"shape": "StringValue"},
                "Count": {"shape": "IntValue"},
            },
        },
        "EchoResponse": {
            "type": "structure",
            "members": {
                "Echo": {"shape": "StringValue"},
                "Doubled": {"shape": "IntValue"},
            },
        },
        "StringValue": {"type": "string"},
        "IntValue": {"type": "integer"},
    },
}

# Model with list/map shapes to test unsupported-type behaviour.
MODEL_WITH_LIST = {
    "metadata": {
        "endpointPrefix": "listsvc",
        "protocol": "rest-json",
        "apiVersion": "2024-01-01",
        "signatureVersion": "v4",
    },
    "operations": {
        "GetItems": {
            "name": "GetItems",
            "http": {"method": "GET", "requestUri": "/items"},
            "input": {"shape": "GetItemsRequest"},
            "output": {"shape": "GetItemsResponse"},
        }
    },
    "shapes": {
        "GetItemsRequest": {
            "type": "structure",
            "members": {"Filter": {"shape": "StringList"}},
        },
        "GetItemsResponse": {
            "type": "structure",
            "members": {"Items": {"shape": "StringList"}},
        },
        "StringList": {"type": "list", "member": {"shape": "StringValue"}},
        "StringValue": {"type": "string"},
    },
}

# Model with location annotations (URI, header, querystring).
MODEL_WITH_LOCATIONS = {
    "metadata": {
        "endpointPrefix": "s3fake",
        "protocol": "rest-xml",
        "apiVersion": "2006-03-01",
        "signatureVersion": "s3",
    },
    "operations": {
        "GetObject": {
            "name": "GetObject",
            "http": {"method": "GET", "requestUri": "/{Bucket}/{Key+}"},
            "input": {"shape": "GetObjectRequest"},
            "output": {"shape": "GetObjectOutput"},
        }
    },
    "shapes": {
        "GetObjectRequest": {
            "type": "structure",
            "required": ["Bucket", "Key"],
            "members": {
                "Bucket": {
                    "shape": "BucketName",
                    "location": "uri",
                    "locationName": "Bucket",
                },
                "Key": {
                    "shape": "ObjectKey",
                    "location": "uri",
                    "locationName": "Key",
                },
                "IfMatch": {
                    "shape": "IfMatch",
                    "location": "header",
                    "locationName": "If-Match",
                },
                "VersionId": {
                    "shape": "ObjectVersionId",
                    "location": "querystring",
                    "locationName": "versionId",
                },
            },
        },
        "GetObjectOutput": {
            "type": "structure",
            "members": {
                "ContentLength": {"shape": "ContentLength"},
                "ContentType": {"shape": "ContentType"},
            },
        },
        "BucketName": {"type": "string"},
        "ObjectKey": {"type": "string"},
        "IfMatch": {"type": "string"},
        "ObjectVersionId": {"type": "string"},
        "ContentLength": {"type": "long"},
        "ContentType": {"type": "string"},
    },
}

# Model with all scalar types to verify the full type-mapping table.
MODEL_ALL_SCALARS = {
    "metadata": {
        "endpointPrefix": "scalarsvc",
        "protocol": "rest-json",
        "apiVersion": "2024-01-01",
        "signatureVersion": "v4",
    },
    "operations": {
        "AllTypes": {
            "name": "AllTypes",
            "http": {"method": "POST", "requestUri": "/all"},
            "input": {"shape": "AllTypesRequest"},
            "output": {"shape": "AllTypesResponse"},
        }
    },
    "shapes": {
        "AllTypesRequest": {
            "type": "structure",
            "members": {
                "AStr": {"shape": "AString"},
                "AnInt": {"shape": "AnInteger"},
                "ALong": {"shape": "ALong"},
                "ABool": {"shape": "ABoolean"},
                "ABlob": {"shape": "ABlob"},
                "ATimestamp": {"shape": "ATimestamp"},
            },
        },
        "AllTypesResponse": {
            "type": "structure",
            "members": {"Status": {"shape": "AString"}},
        },
        "AString": {"type": "string"},
        "AnInteger": {"type": "integer"},
        "ALong": {"type": "long"},
        "ABoolean": {"type": "boolean"},
        "ABlob": {"type": "blob"},
        "ATimestamp": {"type": "timestamp"},
    },
}


# ---------------------------------------------------------------------------
# Unit tests: scalar type mapping
# ---------------------------------------------------------------------------

class TestBotocoreScalarToPie:

    def test_string_maps_to_str(self):
        assert _botocore_scalar_to_pie("string", "SomeName") == "str"

    def test_integer_maps_to_int(self):
        assert _botocore_scalar_to_pie("integer", "SomeName") == "int"

    def test_long_maps_to_int(self):
        assert _botocore_scalar_to_pie("long", "SomeName") == "int"

    def test_boolean_maps_to_int(self):
        assert _botocore_scalar_to_pie("boolean", "SomeName") == "int"

    def test_blob_maps_to_str(self):
        assert _botocore_scalar_to_pie("blob", "SomeName") == "str"

    def test_timestamp_maps_to_int(self):
        assert _botocore_scalar_to_pie("timestamp", "SomeName") == "int"

    def test_unknown_type_raises(self):
        with pytest.raises(TypeError, match="Unknown botocore type"):
            _botocore_scalar_to_pie("float", "BadShape")

    def test_compound_type_raises(self):
        with pytest.raises(TypeError, match="compound type"):
            _botocore_scalar_to_pie("structure", "SomeStruct")

    def test_list_compound_raises(self):
        with pytest.raises(TypeError, match="compound type"):
            _botocore_scalar_to_pie("list", "SomeList")


# ---------------------------------------------------------------------------
# Unit tests: reachability
# ---------------------------------------------------------------------------

class TestCollectReachableShapes:

    def test_all_shapes_reachable(self):
        reachable = _collect_reachable_shapes(MINIMAL_MODEL, operation_names=None)
        assert "EchoRequest" in reachable
        assert "EchoResponse" in reachable
        assert "StringValue" in reachable
        assert "IntValue" in reachable

    def test_filtered_to_selected_operation(self):
        # Provide only one operation name — should still pull its shapes.
        reachable = _collect_reachable_shapes(MINIMAL_MODEL, operation_names=["Echo"])
        assert "EchoRequest" in reachable
        assert "EchoResponse" in reachable

    def test_unknown_operation_raises(self):
        with pytest.raises(SystemExit):
            _collect_reachable_shapes(MINIMAL_MODEL, operation_names=["DoesNotExist"])

    def test_list_shapes_are_reachable_too(self):
        reachable = _collect_reachable_shapes(MODEL_WITH_LIST, operation_names=None)
        assert "StringList" in reachable
        assert "StringValue" in reachable


# ---------------------------------------------------------------------------
# Unit tests: BotocoreModelParser.parse() — minimal model
# ---------------------------------------------------------------------------

class TestParserMinimalModel:

    def setup_method(self):
        self.parser = BotocoreModelParser(MINIMAL_MODEL)
        self.parsed = self.parser.parse()

    def test_service_metadata(self):
        svc = self.parsed["service"]
        assert svc["endpoint_prefix"] == "myservice"
        assert svc["protocol"] == "rest-json"
        assert svc["api_version"] == "2024-01-01"
        assert svc["signature_version"] == "v4"
        assert svc["service_id"] == "MyService"

    def test_operations_list(self):
        ops = self.parsed["operations"]
        assert len(ops) == 1
        echo_op = ops[0]
        assert echo_op["name"] == "Echo"
        assert echo_op["http_method"] == "POST"
        assert echo_op["http_uri"] == "/echo"
        assert echo_op["input_shape"] == "EchoRequest"
        assert echo_op["output_shape"] == "EchoResponse"
        assert echo_op["error_shapes"] == []

    def test_structure_shapes_emitted(self):
        shapes = self.parsed["shapes"]
        assert "EchoRequest" in shapes
        assert "EchoResponse" in shapes

    def test_scalar_aliases_emitted(self):
        shapes = self.parsed["shapes"]
        assert "StringValue" in shapes
        assert shapes["StringValue"]["pie_type"] == "str"
        assert "IntValue" in shapes
        assert shapes["IntValue"]["pie_type"] == "int"

    def test_request_structure_members(self):
        req = self.parsed["shapes"]["EchoRequest"]
        assert req["pie_type"] == "class"
        members = req["members"]
        assert "Message" in members
        assert members["Message"]["pie_type"] == "str"
        assert members["Message"]["required"] is True
        assert "Count" in members
        assert members["Count"]["pie_type"] == "int"
        assert members["Count"]["required"] is False

    def test_response_structure_members(self):
        resp = self.parsed["shapes"]["EchoResponse"]
        members = resp["members"]
        assert "Echo" in members
        assert members["Echo"]["pie_type"] == "str"
        assert "Doubled" in members
        assert members["Doubled"]["pie_type"] == "int"

    def test_required_members_list(self):
        req = self.parsed["shapes"]["EchoRequest"]
        assert "Message" in req["required_members"]
        assert "Count" not in req["required_members"]


# ---------------------------------------------------------------------------
# Unit tests: location annotations
# ---------------------------------------------------------------------------

class TestParserLocationAnnotations:

    def setup_method(self):
        parser = BotocoreModelParser(MODEL_WITH_LOCATIONS)
        self.parsed = parser.parse()

    def test_uri_location(self):
        members = self.parsed["shapes"]["GetObjectRequest"]["members"]
        assert members["Bucket"]["location"] == "uri"
        assert members["Bucket"]["location_name"] == "Bucket"
        assert members["Key"]["location"] == "uri"

    def test_header_location(self):
        members = self.parsed["shapes"]["GetObjectRequest"]["members"]
        assert members["IfMatch"]["location"] == "header"
        assert members["IfMatch"]["location_name"] == "If-Match"

    def test_querystring_location(self):
        members = self.parsed["shapes"]["GetObjectRequest"]["members"]
        assert members["VersionId"]["location"] == "querystring"
        assert members["VersionId"]["location_name"] == "versionId"

    def test_body_default_location(self):
        # GetObjectOutput members have no location annotation — default to "body"
        members = self.parsed["shapes"]["GetObjectOutput"]["members"]
        assert members["ContentLength"]["location"] == "body"
        assert members["ContentLength"]["location_name"] == "ContentLength"

    def test_long_maps_to_int(self):
        # ContentLength is "long" botocore type
        members = self.parsed["shapes"]["GetObjectOutput"]["members"]
        assert members["ContentLength"]["pie_type"] == "int"


# ---------------------------------------------------------------------------
# Unit tests: all scalar types
# ---------------------------------------------------------------------------

class TestParserAllScalarTypes:

    def setup_method(self):
        parser = BotocoreModelParser(MODEL_ALL_SCALARS)
        self.parsed = parser.parse()

    def _member(self, field_name: str) -> dict:
        return self.parsed["shapes"]["AllTypesRequest"]["members"][field_name]

    def test_string_field(self):
        assert self._member("AStr")["pie_type"] == "str"

    def test_integer_field(self):
        assert self._member("AnInt")["pie_type"] == "int"

    def test_long_field(self):
        assert self._member("ALong")["pie_type"] == "int"

    def test_boolean_field(self):
        assert self._member("ABool")["pie_type"] == "int"

    def test_blob_field(self):
        assert self._member("ABlob")["pie_type"] == "str"

    def test_timestamp_field(self):
        assert self._member("ATimestamp")["pie_type"] == "int"


# ---------------------------------------------------------------------------
# Unit tests: list/map shapes
# ---------------------------------------------------------------------------

class TestParserListShapes:

    def test_list_shape_raises_by_default(self):
        parser = BotocoreModelParser(MODEL_WITH_LIST, skip_unsupported=False)
        with pytest.raises(NotImplementedError, match="unsupported"):
            parser.parse()

    def test_list_shape_skipped_with_flag(self):
        parser = BotocoreModelParser(MODEL_WITH_LIST, skip_unsupported=True)
        parsed = parser.parse()
        # The list-type shape should be absent from the output
        shapes = parsed["shapes"]
        assert "StringList" not in shapes
        # Structure shapes that referenced the list will have had the field skipped too
        # (GetItemsRequest had only 'Filter' which is a list — so no members emitted)
        if "GetItemsRequest" in shapes:
            members = shapes["GetItemsRequest"]["members"]
            assert "Filter" not in members


# ---------------------------------------------------------------------------
# Unit tests: operation filtering
# ---------------------------------------------------------------------------

class TestOperationFiltering:

    def test_single_operation_filter(self):
        parser = BotocoreModelParser(MINIMAL_MODEL, operation_names=["Echo"])
        parsed = parser.parse()
        op_names = [op["name"] for op in parsed["operations"]]
        assert op_names == ["Echo"]

    def test_unknown_operation_exits(self):
        with pytest.raises(SystemExit):
            parser = BotocoreModelParser(MINIMAL_MODEL, operation_names=["Nonexistent"])
            parser.parse()


# ---------------------------------------------------------------------------
# Unit tests: invalid model inputs
# ---------------------------------------------------------------------------

class TestInvalidModels:

    def test_missing_metadata_raises(self):
        bad_model = {"operations": {}, "shapes": {}}
        with pytest.raises(RuntimeError, match="missing 'metadata'"):
            BotocoreModelParser(bad_model)

    def test_missing_operations_raises(self):
        bad_model = {"metadata": {"endpointPrefix": "x"}, "shapes": {}}
        with pytest.raises(RuntimeError, match="missing 'operations'"):
            BotocoreModelParser(bad_model)

    def test_missing_shapes_raises(self):
        bad_model = {"metadata": {"endpointPrefix": "x"}, "operations": {}}
        with pytest.raises(RuntimeError, match="missing 'shapes'"):
            BotocoreModelParser(bad_model)


# ---------------------------------------------------------------------------
# Unit tests: .pie code generation
# ---------------------------------------------------------------------------

class TestGeneratePie:

    def setup_method(self):
        parser = BotocoreModelParser(MINIMAL_MODEL)
        self.parsed = parser.parse()
        self.pie = generate_pie(self.parsed, "myservice", "target/myservice.pie")

    def test_header_comment_present(self):
        assert "Auto-generated by scripts/parse_models.py" in self.pie

    def test_service_info_in_comment(self):
        assert "MyService" in self.pie
        assert "rest-json" in self.pie

    def test_request_class_emitted(self):
        assert "class EchoRequest:" in self.pie

    def test_response_class_emitted(self):
        assert "class EchoResponse:" in self.pie

    def test_field_types_in_class(self):
        # Request has Message: str and Count: int
        assert "Message: str" in self.pie
        assert "Count: int" in self.pie

    def test_operation_stub_emitted(self):
        assert "def Echo(" in self.pie

    def test_http_annotation_comment(self):
        assert "HTTP POST /echo" in self.pie

    def test_operation_has_endpoint_param(self):
        # All stubs take 'endpoint: str' as first parameter
        assert "def Echo(endpoint: str" in self.pie

    def test_pie_is_valid_python(self):
        # The generated .pie must be parseable as Python 3
        import ast
        try:
            ast.parse(self.pie)
        except SyntaxError as exc:
            pytest.fail(f"Generated .pie is not valid Python 3: {exc}\n\n{self.pie}")


class TestGeneratePieWithLocations:

    def setup_method(self):
        parser = BotocoreModelParser(MODEL_WITH_LOCATIONS)
        self.parsed = parser.parse()
        self.pie = generate_pie(self.parsed, "s3fake", "target/s3fake.pie")

    def test_get_object_stub(self):
        assert "def GetObject(" in self.pie

    def test_http_method_comment(self):
        assert "HTTP GET" in self.pie

    def test_pie_is_valid_python(self):
        import ast
        try:
            ast.parse(self.pie)
        except SyntaxError as exc:
            pytest.fail(f"Generated .pie is not valid Python 3: {exc}\n\n{self.pie}")


# ---------------------------------------------------------------------------
# Unit tests: JSON metadata generation
# ---------------------------------------------------------------------------

class TestGenerateJson:

    def setup_method(self):
        parser = BotocoreModelParser(MINIMAL_MODEL)
        self.parsed = parser.parse()
        self.json_out = generate_json(self.parsed)
        self.data = json.loads(self.json_out)

    def test_json_is_valid(self):
        # Already parsed in setup_method without error
        assert isinstance(self.data, dict)

    def test_service_present(self):
        assert "service" in self.data

    def test_operations_present(self):
        assert "operations" in self.data
        assert len(self.data["operations"]) == 1

    def test_shapes_present(self):
        assert "shapes" in self.data
        assert "EchoRequest" in self.data["shapes"]

    def test_round_trip_stable(self):
        # Parsing the JSON again must produce identical output.
        second = json.loads(generate_json(self.parsed))
        assert second == self.data


# ---------------------------------------------------------------------------
# Integration tests: live botocore data (STS GetCallerIdentity)
# ---------------------------------------------------------------------------

class TestLiveBotocoreSTS:
    """Verify the parser against the real STS service-2.json."""

    def setup_method(self):
        model = load_model_by_service_name("sts")
        self.parser = BotocoreModelParser(
            model,
            operation_names=["GetCallerIdentity"],
            skip_unsupported=True,
        )
        self.parsed = self.parser.parse()

    def test_service_metadata_sts(self):
        svc = self.parsed["service"]
        assert svc["endpoint_prefix"] == "sts"
        assert svc["api_version"] == "2011-06-15"
        assert svc["signature_version"] == "v4"

    def test_get_caller_identity_operation(self):
        ops = self.parsed["operations"]
        assert len(ops) == 1
        op = ops[0]
        assert op["name"] == "GetCallerIdentity"
        assert op["http_method"] == "POST"
        assert op["input_shape"] == "GetCallerIdentityRequest"
        assert op["output_shape"] == "GetCallerIdentityResponse"

    def test_response_has_string_fields(self):
        shapes = self.parsed["shapes"]
        assert "GetCallerIdentityResponse" in shapes
        resp = shapes["GetCallerIdentityResponse"]
        assert resp["pie_type"] == "class"
        members = resp["members"]
        # UserId, Account, Arn are all string aliases
        for field_name in ("UserId", "Account", "Arn"):
            assert field_name in members, f"Missing field: {field_name}"
            assert members[field_name]["pie_type"] == "str", (
                f"{field_name} should be str, got {members[field_name]['pie_type']}"
            )

    def test_request_is_empty_structure(self):
        shapes = self.parsed["shapes"]
        assert "GetCallerIdentityRequest" in shapes
        req = shapes["GetCallerIdentityRequest"]
        assert req["pie_type"] == "class"
        assert req["members"] == {}

    def test_generated_pie_is_valid_python(self):
        import ast
        pie = generate_pie(self.parsed, "sts", "target/sts.pie")
        try:
            ast.parse(pie)
        except SyntaxError as exc:
            pytest.fail(f"STS .pie is not valid Python 3: {exc}\n\n{pie}")

    def test_generated_pie_contains_class(self):
        pie = generate_pie(self.parsed, "sts", "target/sts.pie")
        assert "class GetCallerIdentityResponse:" in pie
        assert "UserId: str" in pie


class TestLiveBotocoreSTSAssumeRole:
    """Verify AssumeRole with skip_unsupported (it has list-type members)."""

    def setup_method(self):
        model = load_model_by_service_name("sts")
        self.parser = BotocoreModelParser(
            model,
            operation_names=["AssumeRole"],
            skip_unsupported=True,
        )
        self.parsed = self.parser.parse()

    def test_assume_role_operation_present(self):
        ops = self.parsed["operations"]
        assert len(ops) == 1
        assert ops[0]["name"] == "AssumeRole"

    def test_required_members_preserved(self):
        shapes = self.parsed["shapes"]
        assert "AssumeRoleRequest" in shapes
        req_members = shapes["AssumeRoleRequest"]["required_members"]
        # RoleArn and RoleSessionName are required
        assert "RoleArn" in req_members
        assert "RoleSessionName" in req_members

    def test_list_members_skipped(self):
        # Tags and PolicyArns are list-type members — they must be absent
        shapes = self.parsed["shapes"]
        if "AssumeRoleRequest" in shapes:
            members = shapes["AssumeRoleRequest"]["members"]
            assert "Tags" not in members
            assert "PolicyArns" not in members

    def test_string_members_preserved(self):
        shapes = self.parsed["shapes"]
        if "AssumeRoleRequest" in shapes:
            members = shapes["AssumeRoleRequest"]["members"]
            assert "RoleArn" in members
            assert members["RoleArn"]["pie_type"] == "str"

    def test_integer_member_preserved(self):
        shapes = self.parsed["shapes"]
        if "AssumeRoleRequest" in shapes:
            members = shapes["AssumeRoleRequest"]["members"]
            # DurationSeconds is integer
            if "DurationSeconds" in members:
                assert members["DurationSeconds"]["pie_type"] == "int"


# ---------------------------------------------------------------------------
# Integration tests: load_model_from_file with a temp file
# ---------------------------------------------------------------------------

class TestLoadModelFromFile:

    def test_load_minimal_model_from_file(self, tmp_path):
        model_file = tmp_path / "service-2.json"
        model_file.write_text(json.dumps(MINIMAL_MODEL), encoding="utf-8")
        model = load_model_from_file(str(model_file))
        assert "metadata" in model
        assert model["metadata"]["endpointPrefix"] == "myservice"

    def test_missing_file_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            load_model_from_file(str(tmp_path / "no_such_file.json"))

    def test_parse_from_file(self, tmp_path):
        model_file = tmp_path / "service-2.json"
        model_file.write_text(json.dumps(MINIMAL_MODEL), encoding="utf-8")
        model = load_model_from_file(str(model_file))
        parser = BotocoreModelParser(model)
        parsed = parser.parse()
        assert parsed["service"]["endpoint_prefix"] == "myservice"


# ---------------------------------------------------------------------------
# Integration tests: unknown service exits cleanly
# ---------------------------------------------------------------------------

class TestLoadModelByServiceName:

    def test_unknown_service_exits(self):
        with pytest.raises(SystemExit):
            load_model_by_service_name("not-a-real-service-xyzzy")

    def test_known_service_loads(self):
        model = load_model_by_service_name("sts")
        assert "metadata" in model
        assert model["metadata"]["endpointPrefix"] == "sts"
