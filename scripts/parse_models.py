"""Parse a botocore service-2.json model and generate .pie type definitions.

Usage:
    # Parse a botocore service by name (uses installed botocore data directory):
    python scripts/parse_models.py sts --output target/sts.pie

    # Parse a raw service-2.json file directly:
    python scripts/parse_models.py /path/to/service-2.json --output target/svc.pie

    # Emit a JSON metadata file instead of .pie source:
    python scripts/parse_models.py sts --format json --output target/sts_sdk.json

    # Restrict to specific operations:
    python scripts/parse_models.py sts --operations AssumeRole GetCallerIdentity

    # List all available botocore services:
    python scripts/parse_models.py --list-services

Design decisions:
    - Botocore type -> .pie type mapping:
        string    -> str
        integer   -> int
        long      -> int
        boolean   -> int    (0 == false, 1 == true)
        blob      -> str    (raw bytes treated as opaque buffer, same repr)
        timestamp -> int    (Unix epoch seconds)
        structure -> class
        list      -> UNSUPPORTED (raises; .pie has no list type yet)
        map       -> UNSUPPORTED (raises; .pie has no map type yet)

    - Only "structure" shapes that are referenced as operation inputs/outputs
      (and their transitive member shapes) are emitted.  Unreachable shapes
      are ignored.

    - Member location annotations (uri, header, querystring) are preserved in
      the JSON metadata output for future HTTP serialization codegen.

    - Required member lists are preserved in metadata output.

    - No silent fallbacks.  Every unrecognised shape type raises immediately.
"""

import argparse
import json
import os
import sys
from typing import Any


# ---------------------------------------------------------------------------
# Botocore type -> .pie type
# ---------------------------------------------------------------------------

# Scalar botocore types that map 1-to-1 to .pie primitive types.
_SCALAR_TYPE_MAP: dict[str, str] = {
    "string":    "str",
    "integer":   "int",
    "long":      "int",
    "boolean":   "int",   # 0 = false, 1 = true
    "blob":      "str",   # raw bytes, same in-memory repr as str
    "timestamp": "int",   # Unix epoch seconds
}

# Shape types that require nested structure (not directly emittable as a .pie field).
_COMPOUND_TYPES = {"structure", "list", "map"}


def _botocore_scalar_to_pie(botocore_type: str, shape_name: str) -> str:
    """Map a scalar botocore type to its .pie equivalent.

    Raises TypeError for compound or unknown types.
    """
    if botocore_type in _COMPOUND_TYPES:
        raise TypeError(
            f"Shape '{shape_name}' has compound type '{botocore_type}': "
            "resolve to a scalar before calling _botocore_scalar_to_pie."
        )
    if botocore_type not in _SCALAR_TYPE_MAP:
        raise TypeError(
            f"Unknown botocore type '{botocore_type}' on shape '{shape_name}'. "
            "Add it to _SCALAR_TYPE_MAP or handle it explicitly."
        )
    return _SCALAR_TYPE_MAP[botocore_type]


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def load_model_from_file(path: str) -> dict[str, Any]:
    """Load and parse a service-2.json file from disk."""
    if not os.path.exists(path):
        sys.exit(f"Error: model file not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_model_by_service_name(service_name: str) -> dict[str, Any]:
    """Load the latest botocore model for *service_name* using the installed botocore."""
    from botocore.loaders import Loader
    loader = Loader()
    available = loader.list_available_services("service-2")
    if service_name not in available:
        sys.exit(
            f"Error: service '{service_name}' not found in botocore data directory.\n"
            f"Available services: {', '.join(sorted(available))}"
        )
    return loader.load_service_model(service_name, "service-2")


# ---------------------------------------------------------------------------
# Reachability: collect only the shapes actually needed
# ---------------------------------------------------------------------------

def _collect_reachable_shapes(
    model: dict[str, Any],
    operation_names: list[str] | None,
) -> set[str]:
    """Walk the operation graph and return the set of shape names reachable
    from the selected operations' inputs and outputs (and their errors).

    If *operation_names* is None, all operations are included.
    """
    ops = model["operations"]
    shapes = model["shapes"]

    if operation_names is not None:
        unknown = set(operation_names) - set(ops)
        if unknown:
            sys.exit(
                f"Error: unknown operation(s): {', '.join(sorted(unknown))}.\n"
                f"Available: {', '.join(sorted(ops))}"
            )
        selected_ops = {k: v for k, v in ops.items() if k in operation_names}
    else:
        selected_ops = ops

    reachable: set[str] = set()

    def _walk(shape_name: str) -> None:
        if shape_name in reachable:
            return
        if shape_name not in shapes:
            raise RuntimeError(
                f"Shape '{shape_name}' referenced but not defined in the model."
            )
        reachable.add(shape_name)
        shape = shapes[shape_name]
        shape_type = shape["type"]

        if shape_type == "structure":
            for member_info in shape.get("members", {}).values():
                _walk(member_info["shape"])
        elif shape_type == "list":
            _walk(shape["member"]["shape"])
        elif shape_type == "map":
            _walk(shape["key"]["shape"])
            _walk(shape["value"]["shape"])
        # scalar types: no nested shapes to walk

    for op_def in selected_ops.values():
        if "input" in op_def:
            _walk(op_def["input"]["shape"])
        if "output" in op_def:
            _walk(op_def["output"]["shape"])
        for err in op_def.get("errors", []):
            _walk(err["shape"])

    return reachable


# ---------------------------------------------------------------------------
# Shape resolution: compute the effective .pie type for any shape name
# ---------------------------------------------------------------------------

def _resolve_pie_type(
    shape_name: str,
    shapes: dict[str, Any],
    struct_names: set[str],
) -> str:
    """Return the .pie type token for *shape_name*.

    - scalar aliases   -> the .pie primitive ("str", "int")
    - structure shapes -> the Python class name (same as shape_name)
    - list / map       -> raises NotImplementedError (not supported yet)
    """
    if shape_name not in shapes:
        raise RuntimeError(f"Shape '{shape_name}' not found in model shapes dict.")

    shape = shapes[shape_name]
    shape_type = shape["type"]

    if shape_type == "structure":
        return shape_name  # class name in .pie

    if shape_type == "list":
        raise NotImplementedError(
            f"Shape '{shape_name}' is a list — .pie does not support list types yet. "
            "Skip or filter this shape."
        )

    if shape_type == "map":
        raise NotImplementedError(
            f"Shape '{shape_name}' is a map — .pie does not support map types yet. "
            "Skip or filter this shape."
        )

    # scalar alias
    return _botocore_scalar_to_pie(shape_type, shape_name)


# ---------------------------------------------------------------------------
# Core parser
# ---------------------------------------------------------------------------

class BotocoreModelParser:
    """Parse a botocore service-2.json dict and produce structured output."""

    def __init__(
        self,
        model: dict[str, Any],
        operation_names: list[str] | None = None,
        skip_unsupported: bool = False,
    ) -> None:
        self._model = model
        self._operation_names = operation_names
        self._skip_unsupported = skip_unsupported

        metadata = model.get("metadata")
        if not isinstance(metadata, dict):
            raise RuntimeError("Model is missing 'metadata' dict — not a valid service-2.json.")
        if "operations" not in model:
            raise RuntimeError("Model is missing 'operations' — not a valid service-2.json.")
        if "shapes" not in model:
            raise RuntimeError("Model is missing 'shapes' — not a valid service-2.json.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse(self) -> dict[str, Any]:
        """Return a structured dict with service metadata, operations, and shapes.

        Return schema:
        {
          "service": {
            "endpoint_prefix": str,
            "protocol": str,
            "api_version": str,
            "signature_version": str,
            "service_id": str,           # may be absent
            "global_endpoint": str,      # may be absent
          },
          "operations": [
            {
              "name": str,
              "http_method": str,
              "http_uri": str,
              "input_shape": str | None,
              "output_shape": str | None,
              "error_shapes": [str, ...],
            },
            ...
          ],
          "shapes": {
            "<ShapeName>": {
              "pie_type": str,           # "class" | "str" | "int"
              # if pie_type == "class":
              "members": {
                "<FieldName>": {
                  "pie_type": str,       # "str" | "int" | "<ClassName>"
                  "required": bool,
                  "location": str,       # "body" | "uri" | "header" | "querystring"
                  "location_name": str,  # wire name (may differ from field name)
                },
                ...
              },
              "required_members": [str, ...],
            },
            ...
          }
        }
        """
        meta = self._model["metadata"]
        shapes = self._model["shapes"]

        reachable = _collect_reachable_shapes(self._model, self._operation_names)

        service_info = self._parse_service_metadata(meta)
        operations = self._parse_operations(reachable)
        shape_defs = self._parse_shapes(reachable, shapes)

        return {
            "service": service_info,
            "operations": operations,
            "shapes": shape_defs,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_service_metadata(self, meta: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {
            "endpoint_prefix": meta.get("endpointPrefix", ""),
            "protocol": meta.get("protocol") or (meta.get("protocols", [None])[0] or ""),
            "api_version": meta.get("apiVersion", ""),
            "signature_version": meta.get("signatureVersion", "v4"),
        }
        if "serviceId" in meta:
            result["service_id"] = meta["serviceId"]
        if "globalEndpoint" in meta:
            result["global_endpoint"] = meta["globalEndpoint"]
        if "signingName" in meta:
            result["signing_name"] = meta["signingName"]
        return result

    def _parse_operations(self, reachable: set[str]) -> list[dict[str, Any]]:
        ops = self._model["operations"]
        selected = (
            {k: v for k, v in ops.items() if k in (self._operation_names or ops)}
        )
        result = []
        for op_name, op_def in sorted(selected.items()):
            http = op_def.get("http", {})
            entry: dict[str, Any] = {
                "name": op_name,
                "http_method": http.get("method", "POST"),
                "http_uri": http.get("requestUri", "/"),
                "input_shape": op_def["input"]["shape"] if "input" in op_def else None,
                "output_shape": op_def["output"]["shape"] if "output" in op_def else None,
                "error_shapes": [e["shape"] for e in op_def.get("errors", [])],
            }
            result.append(entry)
        return result

    def _parse_shapes(
        self,
        reachable: set[str],
        shapes: dict[str, Any],
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}

        for shape_name in sorted(reachable):
            shape = shapes[shape_name]
            shape_type = shape["type"]

            if shape_type == "structure":
                entry = self._parse_structure_shape(shape_name, shape, shapes)
                result[shape_name] = entry

            elif shape_type in ("list", "map"):
                if self._skip_unsupported:
                    continue
                raise NotImplementedError(
                    f"Shape '{shape_name}' has type '{shape_type}' — not supported "
                    "in .pie yet. Re-run with --skip-unsupported to ignore these."
                )

            else:
                # scalar alias — record it so consumers know the .pie type
                try:
                    pie = _botocore_scalar_to_pie(shape_type, shape_name)
                except TypeError as exc:
                    if self._skip_unsupported:
                        continue
                    raise
                result[shape_name] = {"pie_type": pie}

        return result

    def _parse_structure_shape(
        self,
        shape_name: str,
        shape: dict[str, Any],
        shapes: dict[str, Any],
    ) -> dict[str, Any]:
        required_set = set(shape.get("required", []))
        members_out: dict[str, Any] = {}

        for field_name, member_info in shape.get("members", {}).items():
            member_shape_name = member_info["shape"]

            try:
                pie_type = _resolve_pie_type(member_shape_name, shapes, set())
            except NotImplementedError:
                if self._skip_unsupported:
                    continue
                raise NotImplementedError(
                    f"Member '{shape_name}.{field_name}' references unsupported "
                    f"shape '{member_shape_name}'. Re-run with --skip-unsupported to skip."
                )

            location = member_info.get("location", "body")
            location_name = member_info.get("locationName", field_name)

            members_out[field_name] = {
                "pie_type": pie_type,
                "required": field_name in required_set,
                "location": location,
                "location_name": location_name,
            }

        return {
            "pie_type": "class",
            "members": members_out,
            "required_members": sorted(required_set & set(members_out)),
        }


# ---------------------------------------------------------------------------
# .pie code generator
# ---------------------------------------------------------------------------

_PIE_HEADER = """\
# Auto-generated by scripts/parse_models.py from botocore model.
# Service: {service_id}  Protocol: {protocol}  API version: {api_version}
# DO NOT EDIT BY HAND — regenerate with:
#   python scripts/parse_models.py {service_name} --output {output_path}
"""

_PIE_STUB_COMMENT = """\
# --- Operation stubs ---
# These are placeholder function signatures.
# Actual HTTP dispatch will be implemented in .pie once SigV4 is ready.
"""


def _sanitize_field_name(name: str) -> str:
    """Lower-case the first character so Python field names are idiomatic.

    Botocore member names are PascalCase (e.g. 'RoleArn'). We keep them
    as-is to match the wire names exactly; valid .pie class fields may be any
    identifier.  This keeps round-tripping easier.
    """
    return name


def generate_pie(
    parsed: dict[str, Any],
    service_name: str,
    output_path: str,
) -> str:
    """Render the parsed model as a .pie source string."""
    service = parsed["service"]
    shapes = parsed["shapes"]
    operations = parsed["operations"]

    lines: list[str] = []

    # Header comment
    lines.append(_PIE_HEADER.format(
        service_id=service.get("service_id", service_name),
        protocol=service["protocol"],
        api_version=service["api_version"],
        service_name=service_name,
        output_path=output_path,
    ).rstrip())
    lines.append("")

    # Emit class definitions for structure shapes only
    structure_shapes = {
        name: defn
        for name, defn in sorted(shapes.items())
        if defn.get("pie_type") == "class"
    }

    if structure_shapes:
        lines.append("# --- Type definitions ---")
        lines.append("")

    for class_name, class_def in structure_shapes.items():
        lines.append(f"class {class_name}:")
        members = class_def.get("members", {})
        required_members = set(class_def.get("required_members", []))

        if not members:
            lines.append("    pass")
        else:
            # Required members first, then optional (sorted within each group)
            req_fields = sorted(
                [(n, m) for n, m in members.items() if n in required_members],
                key=lambda t: t[0],
            )
            opt_fields = sorted(
                [(n, m) for n, m in members.items() if n not in required_members],
                key=lambda t: t[0],
            )

            for field_name, field_info in req_fields + opt_fields:
                pie_type = field_info["pie_type"]
                lines.append(f"    {field_name}: {pie_type}")

        lines.append("")

    # Emit operation stub functions
    if operations:
        lines.append(_PIE_STUB_COMMENT.rstrip())
        lines.append("")

    for op in operations:
        op_name = op["name"]
        input_shape = op["input_shape"]
        output_shape = op["output_shape"]
        http_method = op["http_method"]
        http_uri = op["http_uri"]

        # Build parameter list from input shape members (required first)
        params = ["endpoint: str"]
        if input_shape and input_shape in structure_shapes:
            input_def = structure_shapes[input_shape]
            members = input_def.get("members", {})
            required_set = set(input_def.get("required_members", []))
            req = sorted([(n, m) for n, m in members.items() if n in required_set])
            opt = sorted([(n, m) for n, m in members.items() if n not in required_set])
            for field_name, field_info in req + opt:
                params.append(f"{field_name}: {field_info['pie_type']}")
        elif input_shape:
            # input shape was a scalar alias or unsupported
            pie_type = shapes.get(input_shape, {}).get("pie_type", "str")
            if pie_type not in ("class",):
                params.append(f"request: {pie_type}")

        return_type = "int"  # default to int (status code) if no output
        if output_shape and output_shape in structure_shapes:
            return_type = output_shape
        elif output_shape and output_shape in shapes:
            return_type = shapes[output_shape].get("pie_type", "int")

        param_str = ", ".join(params)
        lines.append(f"# HTTP {http_method} {http_uri}")
        lines.append(f"def {op_name}({param_str}) -> {return_type}:")
        lines.append(f"    pass  # TODO: implement SigV4 + HTTP dispatch")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# JSON metadata output
# ---------------------------------------------------------------------------

def generate_json(parsed: dict[str, Any]) -> str:
    """Serialise the parsed model to a JSON string."""
    return json.dumps(parsed, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse a botocore service-2.json model and generate .pie SDK stubs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "service",
        nargs="?",
        help=(
            "Service name (e.g. 'sts') or path to a service-2.json file. "
            "Omit when using --list-services."
        ),
    )
    parser.add_argument(
        "--output", "-o",
        default=None,
        help="Output file path (.pie or .json). Defaults to stdout.",
    )
    parser.add_argument(
        "--format",
        choices=["pie", "json"],
        default="pie",
        help="Output format: 'pie' (default) or 'json' metadata.",
    )
    parser.add_argument(
        "--operations",
        nargs="+",
        metavar="OPERATION",
        default=None,
        help="Restrict output to these operations (default: all).",
    )
    parser.add_argument(
        "--skip-unsupported",
        action="store_true",
        help="Skip list/map shapes instead of raising an error.",
    )
    parser.add_argument(
        "--list-services",
        action="store_true",
        help="Print all botocore service names and exit.",
    )
    args = parser.parse_args()

    if args.list_services:
        from botocore.loaders import Loader
        loader = Loader()
        services = loader.list_available_services("service-2")
        print("\n".join(sorted(services)))
        return

    if not args.service:
        parser.error("Provide a service name or --list-services.")

    # Load model
    if os.path.exists(args.service) and args.service.endswith(".json"):
        model = load_model_from_file(args.service)
        service_name = os.path.splitext(os.path.basename(args.service))[0]
    else:
        model = load_model_by_service_name(args.service)
        service_name = args.service

    # Parse
    model_parser = BotocoreModelParser(
        model,
        operation_names=args.operations,
        skip_unsupported=args.skip_unsupported,
    )
    parsed = model_parser.parse()

    # Generate output
    output_path = args.output or f"<stdout>"
    if args.format == "json":
        content = generate_json(parsed)
    else:
        content = generate_pie(parsed, service_name, output_path)

    if args.output:
        os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"Written to {args.output}")
    else:
        print(content)


if __name__ == "__main__":
    main()
