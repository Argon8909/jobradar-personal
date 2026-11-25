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

FIELD_PURPOSES = {
    "profile_data":"Профессиональная квалификация: профессия, навыки, опыт, образование, допуски. Не условия поиска.",
    "criteria":"Условия поиска: география, формат работы, зарплата, занятость, предпочтения и ограничения. После создания профиля первый контекст активен; условия можно сохранить отдельным update_context.",
}


def validate_profile_fields(data):
    """Enforce the documented profile/criteria boundary without changing JSON Schema."""
    criteria_keys = {"work_format_preference","work_format","work_formats","salary","salary_remote","salary_office",
                     "salary_min","salary_expectations","employment_type","employment_preference","relocation",
                     "locations","location","geography","adjacent_roles","target_roles","preferences","restrictions","publication_period","vacancy_max_age_days"}
    for item in data.get("additional_attributes",[]):
        key = item["key"].strip().casefold()
        if key in criteria_keys or key.startswith("salary_"):
            raise ProfileError("VALIDATION_ERROR",
                "Условия поиска (формат работы, география, зарплата, занятость и направления) относятся к критериям контекста. "
                "В профиле оставьте квалификацию; условия сохраните отдельной командой обновления контекста после создания профиля.")


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
    if command in ("create_context","update_context"):
        from jobradar.search.period import stored_period
        try:
            stored_period(clean.get("criteria",{}) if command == "create_context" else clean["changes"])
        except ValueError as exc:
            raise ProfileError("VALIDATION_ERROR",str(exc)) from None
    return clean
