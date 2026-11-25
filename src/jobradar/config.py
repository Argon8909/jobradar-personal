"""Load public configuration; relative paths belong to the configuration file."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Settings:
    config_path: Path
    database_path: Path
    prompts_dir: Path
    busy_timeout_ms: int
    values: dict[str, Any]


def load_settings(path: str | Path) -> Settings:
    config_path = Path(path).expanduser().resolve()
    with config_path.open(encoding="utf-8-sig") as stream:
        values = json.load(stream)
    if not isinstance(values, dict):
        raise ValueError("Configuration must be a JSON object")

    # Explicit opt-in: v0.1's inline search section remains supported unchanged.
    if "search_config" in values:
        if "search" in values:
            raise ValueError("Use search_config or search, not both")
        search_path = values["search_config"]
        if not isinstance(search_path,str) or not search_path.strip():
            raise ValueError("search_config must be a nonempty path")
        search_path = (config_path.parent / Path(search_path).expanduser()).resolve()
        try:
            values["search"] = json.loads(search_path.read_text(encoding="utf-8-sig"))
        except (OSError,ValueError) as error:
            raise ValueError(f"Cannot load search_config {search_path}: {error}") from error
        required_search = {"max_source_vacancies","max_new_analyses","max_llm_calls",
                           "deadline_seconds","target_count","vacancy_max_age_days"}
        if not isinstance(values["search"],dict) or set(values["search"]) != required_search:
            raise ValueError("search_config must contain exactly: " + ", ".join(sorted(required_search)))
        for key in required_search - {"vacancy_max_age_days"}:
            if type(values["search"][key]) is not int or values["search"][key] <= 0:
                raise ValueError(f"search_config.{key} must be a positive integer")

    required = {
        "database": ("path", "busy_timeout_ms"),
        "llm": ("provider", "model", "base_url", "temperature", "timeout_seconds"),
        "dialogue": ("max_tool_calls",),
        "search": ("max_source_vacancies", "max_new_analyses", "deadline_seconds"),
        "source": ("page_timeout_seconds",),
        "retries": ("max_safe_retries",),
        "results": ("default_limit", "max_limit"),
        "web": ("host", "port"),
    }
    for section, keys in required.items():
        if not isinstance(values.get(section), dict):
            raise ValueError(f"Missing configuration section: {section}")
        for key in keys:
            if key not in values[section]:
                raise ValueError(f"Missing configuration setting: {section}.{key}")

    numeric = {
        "database": ("busy_timeout_ms",),
        "llm": ("timeout_seconds",),
        "dialogue": ("max_tool_calls",),
        "search": ("max_source_vacancies", "max_new_analyses", "deadline_seconds"),
        "source": ("page_timeout_seconds",),
        "results": ("default_limit", "max_limit"),
        "web": ("port",),
    }
    for section, keys in numeric.items():
        for key in keys:
            value = values[section][key]
            if type(value) is not int or value <= 0:
                raise ValueError(f"{section}.{key} must be a positive integer")
    retries = values["retries"]["max_safe_retries"]
    if type(retries) is not int or not 0 <= retries <= 2:
        raise ValueError("retries.max_safe_retries must be between 0 and 2")
    if values["dialogue"]["max_tool_calls"] > 20:
        raise ValueError("dialogue.max_tool_calls cannot exceed 20")
    if values["results"]["default_limit"] > values["results"]["max_limit"]:
        raise ValueError("results.default_limit cannot exceed results.max_limit")
    if values["web"]["port"] > 65535:
        raise ValueError("web.port must be at most 65535")
    for section, key in (("llm", "max_tokens"), ("dialogue", "max_llm_calls"),
                         ("search", "max_llm_calls"), ("search", "target_count")):
        value = values[section].get(key)
        if key in values[section] and (type(value) is not int or value <= 0):
            raise ValueError(f"{section}.{key} must be a positive integer")
    if "hh" in values and (not isinstance(values["hh"],dict) or type(values["hh"].get("headless",False)) is not bool):
        raise ValueError("hh.headless must be a boolean")
    age = values["search"].get("vacancy_max_age_days")
    if age is not None and (type(age) is not int or not 1 <= age <= 36500):
        raise ValueError("search.vacancy_max_age_days must be null or an integer between 1 and 36500")
    for section,key in (("hh","manual_action_timeout_seconds"),("source","content_timeout_seconds")):
        value = values.get(section,{}).get(key)
        if key in values.get(section,{}) and (type(value) is not int or value <= 0):
            raise ValueError(f"{section}.{key} must be a positive integer")
    repairs = values["retries"].get("max_structural_repairs",1)
    diagnostics = values.get("diagnostics",{})
    if not isinstance(diagnostics,dict) or diagnostics.get("capture_llm","errors") not in ("off","errors","all"):
        raise ValueError("diagnostics.capture_llm must be off, errors or all")
    for key in ("retention_days","max_response_chars","max_file_bytes"):
        value = diagnostics.get(key)
        if key in diagnostics and (type(value) is not int or value <= 0):
            raise ValueError(f"diagnostics.{key} must be a positive integer")
    if type(repairs) is not int or not 0 <= repairs <= 1:
        raise ValueError("retries.max_structural_repairs must be 0 or 1")
    if values["llm"]["provider"] != "deepseek":
        raise ValueError("Only DeepSeek is supported in v0.1")
    temperature = values["llm"]["temperature"]
    if type(temperature) not in (int, float) or not 0 <= temperature <= 2:
        raise ValueError("llm.temperature must be between 0 and 2")
    for section, key in (("llm", "model"), ("llm", "base_url"), ("web", "host")):
        if not isinstance(values[section][key], str) or not values[section][key].strip():
            raise ValueError(f"{section}.{key} must be a nonempty string")

    def resolve(value: Any, name: str) -> Path:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a nonempty path")
        return (config_path.parent / Path(value).expanduser()).resolve()

    return Settings(
        config_path=config_path,
        database_path=resolve(values["database"]["path"], "database.path"),
        prompts_dir=resolve(values.get("prompts_dir"), "prompts_dir"),
        busy_timeout_ms=values["database"]["busy_timeout_ms"],
        values=values,
    )
