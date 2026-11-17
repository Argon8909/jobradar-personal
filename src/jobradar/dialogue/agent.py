"""Bounded dialogue loop; semantic intent comes from LLM, permission checks from Python."""

import json

from jsonschema import Draft202012Validator, ValidationError

from jobradar.llm.client import LLMError
from jobradar.profiles import ProfileError
from jobradar.profiles.contracts import validate
from jobradar.storage.chat import ChatHistory
from jobradar.tools.dispatcher import ToolDispatcher, canonical, failure
from jobradar.tools.registry import DEFINITIONS, READ_ONLY, definitions

INTENT_SCHEMA = {
    "type":"object", "additionalProperties":False,
    "required":["actions", "accept_proposal", "same_topic"],
    "properties": {
        "accept_proposal":{"type":"boolean"}, "same_topic":{"type":"boolean"},
        "actions":{"type":"array", "maxItems":20, "items":{
            "type":"object", "additionalProperties":False, "required":["name", "arguments", "evidence"],
            "properties":{"name":{"type":"string", "enum":list(DEFINITIONS)},
                          "arguments":{"type":"object"}, "evidence":{"type":"string", "minLength":1}}}}}}

LABELS = {"create_profile":"Создать профиль", "create_context":"Создать контекст",
          "switch_context":"Переключить контекст", "return_to_general":"Вернуться в общий диалог",
          "update_profile":"Обновить профиль", "update_context":"Обновить критерии",
          "search_vacancies":"Запустить поиск"}


def describe(name, arguments):
    values = arguments.get("changes", arguments)
    detail = json.dumps(values, ensure_ascii=False) if values else ""
    return LABELS.get(name, name) + (": " + detail if detail else "")


class DialogueAgent:
    def __init__(self, manager, client, settings, dispatcher=None):
        self.manager = manager
        self.client = client
        self.settings = settings
        self.dispatcher = dispatcher or ToolDispatcher(manager)
        self.history = ChatHistory(manager)
        self.pending = None

    def _complete(self, messages, **kwargs):
        if self._llm_calls >= self.settings.values["dialogue"].get("max_llm_calls", 24):
            raise LLMError("Достигнут лимит обращений к модели для этого запроса.")
        self._llm_calls += 1
        return self.client.complete(messages, **kwargs)

    def _intent(self, text, state, prompt):
        pending = self.pending if self.pending and self.pending["state"] == state else None
        # A technical structured-output task uses the unchanged approved dialogue
        # system prompt. User/history/state remain data in separate user messages.
        task = {
            "task": "Выдели только явно запрошенные пользователем действия. Не добавляй собственные предложения. "
                    "Верни только JSON по output_schema. actions содержит точные аргументы инструментов "
                    "и evidence — дословную цитату намерения из текущего user_message. "
                    "accept_proposal=true только если пользователь согласился с последним предложением; "
                    "тогда само предложение не дублируй в actions. При смене темы same_topic=false. "
                    "Факты о квалификации сохраняй только по явному намерению пользователя.",
            "user_message":text, "state":state, "pending_proposal":pending,
            "recent_dialogue":self.history.dialogue_messages(12),
            "tool_definitions":list(DEFINITIONS.values()), "output_schema":INTENT_SCHEMA}
        answer = self._complete([{"role":"system", "content":prompt},
                                 {"role":"user", "content":json.dumps(task, ensure_ascii=False)}], json_mode=True)
        try:
            intent = json.loads(answer["content"])
            Draft202012Validator(INTENT_SCHEMA).validate(intent)
            actions = [(item["name"], validate(item["name"], item["arguments"])) for item in intent["actions"]]
            if any(not item["evidence"].strip() or item["evidence"] not in text for item in intent["actions"]):
                raise ValueError()
        except (ValueError, KeyError, TypeError, ProfileError, ValidationError):
            raise LLMError("Не удалось надёжно определить намерение. Уточните команду.") from None
        if intent["accept_proposal"] and intent["same_topic"] and pending:
            actions.append((pending["name"], pending["arguments"]))
        self.pending = None
        return {canonical(name, arguments) for name, arguments in actions if name not in READ_ONLY}

    def _capture_proposal(self, answer, state, prompt):
        """Track a concrete proposal expressed as plain assistant text, too."""
        task = {
            "task":"extract_proposal",
            "instruction":"Извлеки последнее конкретное предложение ассистента, требующее согласия. "
                          "Не извлекай уже выполненные действия или общие советы. Верни JSON "
                          "{\"proposal\":null} либо {\"proposal\":{\"name\":имя инструмента,"
                          "\"arguments\":точные аргументы,\"evidence\":дословная цитата из assistant_text}}. "
                          "Не выдумывай неизвестные аргументы.",
            "assistant_text":answer, "state":state, "tools":definitions(state)}
        try:
            response = self._complete([{"role":"system", "content":prompt},
                                       {"role":"user", "content":json.dumps(task, ensure_ascii=False)}], json_mode=True)
            proposal = json.loads(response["content"])["proposal"]
            if proposal is None:
                return
            if not isinstance(proposal, dict) or set(proposal) != {"name", "arguments", "evidence"}:
                return
            name, evidence = proposal["name"], proposal["evidence"]
            available = {tool["function"]["name"] for tool in task["tools"]}
            if not isinstance(name, str) or name not in available or name in READ_ONLY:
                return
            if not isinstance(evidence, str) or not evidence.strip() or evidence not in answer:
                return
            arguments = validate(name, proposal["arguments"])
            self.pending = {"name":name, "arguments":arguments, "state":state}
        except (LLMError, ValueError, KeyError, TypeError):
            # Failure to identify a proposal never grants permission. The user can
            # issue an explicit command instead; no action is executed here.
            return

    def chat(self, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Message must be a nonempty string")
        self._llm_calls = 0
        state = self.manager.get_state()
        prompt = (self.settings.prompts_dir / "dialogue.md").read_text(encoding="utf-8")
        transcript = self.history.dialogue_messages()
        self.history.append([{"role":"user", "content":text}], state["active_context_id"])
        notices, errors, results = [], [], []
        try:
            grants = self._intent(text, state, prompt)
            count, seen = 0, {}
            messages = [{"role":"system", "content":prompt}] + transcript + [{"role":"user", "content":text}]
            while count < self.settings.values["dialogue"]["max_tool_calls"]:
                # Recompute tools for every model request. State checks in Dispatcher
                # also protect the interval between generation and execution.
                current = self.manager.get_state()
                if current != state:
                    grants.clear()
                    self.pending = None
                    state = current
                context = {"state":state, "profiles":self.manager.list_profiles()["profiles"],
                           "authorized_actions":[json.loads(key) for key in sorted(grants)],
                           "availability":"Поиск и результаты вакансий ещё не подключены."}
                response = self._complete(messages + [{"role":"user", "content":json.dumps(context, ensure_ascii=False)}],
                                          tools=definitions(state))
                calls = response.get("tool_calls") or []
                if not calls:
                    answer = response.get("content") or "Уточните, что нужно сделать."
                    break
                if not isinstance(calls, list):
                    raise LLMError("Модель вернула некорректные вызовы инструментов.")
                remaining = self.settings.values["dialogue"]["max_tool_calls"] - count
                calls = calls[:remaining]
                assistant = {"role":"assistant", "content":None, "tool_calls":calls}
                messages.append(assistant)
                stored = [assistant]
                group_context = state["active_context_id"]
                for call in calls:
                    count += 1
                    try:
                        call_id = call["id"]
                        name = call["function"]["name"]
                        if not isinstance(call_id, str) or not call_id or not isinstance(name, str):
                            raise ValueError()
                        arguments = json.loads(call["function"]["arguments"])
                        signature = canonical(name, arguments)
                    except (ValueError, TypeError, KeyError):
                        raise LLMError("Модель вернула некорректный вызов инструмента.") from None
                    if call_id in seen:
                        previous_signature, previous = seen[call_id]
                        result = previous if signature == previous_signature else failure("VALIDATION_ERROR", "ID вызова уже использован.")
                    else:
                        result = self.dispatcher.execute(name, arguments, expected_state=state,
                                                         grant=signature if signature in grants else None)
                        grants.discard(signature)
                        seen[call_id] = (signature, result)
                        if result["status"] == "success":
                            if name not in READ_ONLY:
                                notices.append("Выполнено: " + describe(name, result["data"].get("updated_fields", arguments)))
                                self.pending = None
                            state = self.manager.get_state()
                        elif result["error"]["code"] == "CONFIRMATION_REQUIRED":
                            # Validate before storing the proposal; bind it to the
                            # exact current context/revisions, not just a tool name.
                            validate(name, arguments)
                            self.pending = {"name":name, "arguments":arguments, "state":state}
                        else:
                            errors.append(result["error"]["message"])
                    tool_message = {"role":"tool", "tool_call_id":call_id,
                                    "content":json.dumps(result, ensure_ascii=False)}
                    messages.append(tool_message)
                    stored.append(tool_message)
                    results.append({"name":name, "result":result})
                self.history.append(stored, group_context)
            else:
                answer = "Достигнут лимит команд. Продолжите отдельным сообщением."
            if errors:
                answer = " ".join(dict.fromkeys(errors))
            if not self.pending and not errors and count < self.settings.values["dialogue"]["max_tool_calls"]:
                self._capture_proposal(answer, state, prompt)
            if self.pending:
                answer = "Предлагаю: " + describe(self.pending["name"], self.pending["arguments"]) + ". Подтверждаете?"
            answer = "\n".join(notices + [answer])
        except LLMError as exc:
            answer = "\n".join(notices + [str(exc)])
            self.pending = None
        self.history.append([{"role":"assistant", "content":answer}], self.manager.get_state()["active_context_id"])
        return {"answer":answer, "calls":results, "llm_calls":self._llm_calls,
                "active_context_id":self.manager.get_state()["active_context_id"]}
