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
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError(f"{section}.{key} must be a positive integer")
    if "hh" in values and (not isinstance(values["hh"],dict) or type(values["hh"].get("headless",False)) is not bool):
        raise ValueError("hh.headless must be a boolean")
    for section,key in (("hh","manual_action_timeout_seconds"),("source","content_timeout_seconds")):
        value = values.get(section,{}).get(key)
        if value is not None and (type(value) is not int or value <= 0):
            raise ValueError(f"{section}.{key} must be a positive integer")
    repairs = values["retries"].get("max_structural_repairs",1)
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
