"""DeepSeek transport with bounded safe generation retries and sanitized errors."""

import json
import os
import time
from pathlib import Path
from urllib import error, request
from jobradar.diagnostics import record


class LLMError(RuntimeError):
    def __init__(self, message="Не удалось получить ответ DeepSeek.", *, retryable=False, code="LLM_ERROR", response_content=None):
        super().__init__(message)
        self.retryable = retryable
        self.code = code
        self.response_content = response_content


def read_api_key(env_path: Path) -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key and env_path.exists():
        for line in env_path.read_text(encoding="utf-8-sig").splitlines():
            name, separator, value = line.partition("=")
            if separator and name.strip() == "DEEPSEEK_API_KEY":
                key = value.strip().strip("\"'")
    if not key:
        raise LLMError("Укажите DEEPSEEK_API_KEY в локальном .env.",code="LLM_KEY_MISSING")
    return key


class DeepSeekClient:
    def __init__(self, settings, *, api_key=None, opener=request.urlopen, sleeper=time.sleep):
        self.settings = settings
        self._key = api_key
        self._opener = opener
        self._sleep = sleeper

    def complete(self, messages, *, tools=None, json_mode=False, deadline=None, on_retry=None):
        options = self.settings.values["llm"]
        key = self._key or read_api_key(self.settings.config_path.parent.parent / ".env")
        payload = {"model": options["model"], "messages": messages,
                   "temperature": options["temperature"], "stream": False,
                   "thinking":{"type":"disabled"},
                   "max_tokens": options.get("max_tokens", 3200)}
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        req = request.Request(options["base_url"].rstrip("/") + "/chat/completions",
                              data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(),
                              headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        retries = self.settings.values["retries"]["max_safe_retries"]
        for attempt in range(retries + 1):
            timeout = options["timeout_seconds"]
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LLMError("Достигнут предел времени поисковой задачи.",code="LLM_TIMEOUT")
                timeout = min(timeout,remaining)
            if attempt and on_retry:
                # The caller already charged the first attempt. A transport
                # retry must reserve another call before making another request.
                on_retry()
            try:
                with self._opener(req, timeout=timeout) as response:
                    result = json.load(response)
                choice = result["choices"][0]
                if choice.get("finish_reason") == "length":
                    raise LLMError("Ответ DeepSeek обрезан; действие не выполнено.",code="LLM_TRUNCATED",response_content=choice.get("message",{}).get("content"))
                message = choice["message"]
                if not isinstance(message, dict) or message.get("role") != "assistant":
                    raise ValueError()
                content = message.get("content")
                calls = message.get("tool_calls")
                if content is not None and not isinstance(content, str):
                    raise ValueError()
                if calls is not None and not isinstance(calls, list):
                    raise ValueError()
                if not content and not calls:
                    raise ValueError()
                usage = result.get("usage",{})
                safe_usage = {key:value for key,value in usage.items() if key in ("prompt_tokens","completion_tokens","total_tokens")
                              and type(value) is int and value >= 0} if isinstance(usage,dict) else {}
                record("llm","generation_completed",attempt=attempt+1,**safe_usage)
                return {"role": "assistant", "content": content,"finish_reason":choice.get("finish_reason"),"usage":safe_usage,
                        "transport_attempts":attempt+1,
                        **({"tool_calls": calls} if calls else {})}
            except error.HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code <= 599
                message = {400:"Проверьте модель и параметры запроса DeepSeek.",401:"Проверьте API-ключ DeepSeek.",
                           402:"Проверьте баланс DeepSeek.",403:"DeepSeek отклонил доступ.",
                           429:"Превышен лимит запросов DeepSeek."}.get(exc.code,"Ошибка HTTP при обращении к DeepSeek.")
                exc.close()
                failure = LLMError(message, retryable=retryable,code=f"LLM_HTTP_{exc.code}")
            except (error.URLError, TimeoutError, ConnectionError, OSError):
                failure = LLMError("DeepSeek временно недоступен.", retryable=True,code="LLM_NETWORK")
            except (ValueError, KeyError, IndexError, TypeError):
                raise LLMError("DeepSeek вернул некорректный ответ.",code="LLM_PROTOCOL") from None
            if not failure.retryable or attempt == retries:
                record("llm","generation_failed",attempt=attempt+1,retryable=failure.retryable)
                raise failure from None
            self._sleep(min(0.5 * 2 ** attempt, 2))
