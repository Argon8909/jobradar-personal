"""Validate published inputs without maintaining a second handwritten schema."""

import json
from importlib.resources import files

from jsonschema import Draft202012Validator


class ProfileError(ValueError):
    """Stable domain error; the future Dispatcher maps this to the tool envelope."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.retryable = False


_definitions = json.loads(files("jobradar.tools").joinpath("definitions.json").read_text(encoding="utf-8"))
_validators = {
    item["function"]["name"]: Draft202012Validator(item["function"]["parameters"])
    for item in _definitions
}


def validate(command: str, arguments: dict) -> dict:
    try:
        # Round trip also prevents callers from injecting non-JSON Python objects,
        # NaN or Infinity; it returns a detached copy of caller-owned mutable data.
        clean = json.loads(json.dumps(arguments, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError, OverflowError) as error:
        raise ProfileError("VALIDATION_ERROR", "Arguments must be finite JSON values") from error
    error = next(_validators[command].iter_errors(clean), None)
    if error:
        path = ".".join(map(str, error.absolute_path)) or "arguments"
        raise ProfileError("VALIDATION_ERROR", f"Invalid field: {path}")
    return clean
