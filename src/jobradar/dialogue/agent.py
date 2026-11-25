"""Bounded dialogue loop; semantic intent comes from LLM, permission checks from Python."""

import json
from uuid import uuid4

from jsonschema import Draft202012Validator, ValidationError

from jobradar.llm.client import LLMError
from jobradar.events import collect
from jobradar.profiles import ProfileError
from jobradar.profiles.contracts import FIELD_PURPOSES, validate, validate_profile_fields
from jobradar.storage.chat import ChatHistory
from jobradar.tools.dispatcher import ToolDispatcher, canonical, failure
from jobradar.tools.registry import DEFINITIONS, READ_ONLY, definitions
from .presentation import completed, describe, short_confirmation
from jobradar.search.trace import search_explanation

INTENT_SCHEMA = {
    "type":"object", "additionalProperties":False,
    "required":["actions", "accept_proposal", "same_topic", "explain_search"],
    "properties": {
        "accept_proposal":{"type":"boolean"}, "same_topic":{"type":"boolean"},
        "explain_search":{"type":"boolean"},
        "actions":{"type":"array", "maxItems":20, "items":{
            "type":"object", "additionalProperties":False, "required":["name", "arguments", "evidence"],
            "properties":{"name":{"type":"string", "enum":list(DEFINITIONS)},
                          "arguments":{"type":"object"}, "evidence":{"type":"string", "minLength":1}}}}}}

class DialogueAgent:
    def __init__(self, manager, client, settings, dispatcher=None):
        self.manager = manager
        self.client = client
        self.settings = settings
        self.dispatcher = dispatcher or ToolDispatcher(manager)
        self.history = ChatHistory(manager)
        self.pending = None

    def _consume_call(self):
        if self._llm_calls >= self.settings.values["dialogue"].get("max_llm_calls", 24):
            raise LLMError("Достигнут лимит обращений к модели для этого запроса.")
        self._llm_calls += 1

    def _complete(self, messages, **kwargs):
        self._consume_call()
        return self.client.complete(messages, on_retry=self._consume_call, **kwargs)

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
                    "explain_search=true если пользователь просит объяснить результаты, пропуски, ошибки или причины неоценённых вакансий прошлого поиска. "
                    "Сам вопрос о причинах не означает запрос нового поиска; не включай search_vacancies в actions без явной команды искать. "
                    "Факты о квалификации сохраняй только по явному намерению пользователя. "
                    "Новое название роли не требует нового профиля/контекста. Разовый период или направление "
                    "передаются в request search_vacancies без update_context. Постоянный период сохраняется только "
                    "по явному указанию в дальнейшем/всегда через restrictions: "
                    "{attribute:publication_period,operator:within,value:days:14 (либо weeks:N, months:N, unlimited, default)}. "
                    "Не стирай остальные условия; не заменяй полное предложение вариантом только с названием.",
            "user_message":text, "state":state, "pending_proposal":pending,
            "recent_dialogue":self.history.dialogue_messages(12),
            "field_purposes":FIELD_PURPOSES,
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
            if not actions:
                self._confirmed_proposal = pending
            actions.append((pending["name"], pending["arguments"]))
        if short_confirmation(text) and not pending:
            actions = []  # A bare yes cannot authorize an action recovered from old history.
        self.pending = None
        self._explain_search = bool(intent.get("explain_search") and not actions)
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
            "assistant_text":answer, "state":state, "tools":definitions(state),"field_purposes":FIELD_PURPOSES}
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
            if name in ("create_profile","update_profile"):
                validate_profile_fields(arguments.get("profile_data",{}) if name == "create_profile" else arguments["changes"])
            self.pending = {"name":name, "arguments":arguments, "state":state}
        except (LLMError, ValueError, KeyError, TypeError):
            # Failure to identify a proposal never grants permission. The user can
            # issue an explicit command instead; no action is executed here.
            return

    def _describe(self,name,arguments):
        context_name = None
        if name == "switch_context":
            for profile in self.manager.list_profiles()["profiles"]:
                for context in profile["contexts"]:
                    if context["context_id"] == arguments["context_id"]:
                        context_name = context["name"]
        return describe(name,arguments,context_name=context_name)

    def _finish(self,answer,results):
        active = self.manager.get_state()["active_context_id"]
        self.history.append([{"role":"assistant","content":answer}],active)
        result = {"answer":answer,"calls":results,"llm_calls":self._llm_calls,"active_context_id":active}
        yield {"event":"answer","data":result}
        return result

    @staticmethod
    def _model_result(name, result):
        if name not in ("search_vacancies","get_search_results","get_vacancy_details") or result["status"] != "success":
            return result
        data = dict(result["data"])
        # Detailed skipped lists belong to diagnostics, not to the main answer.
        data.pop("unassessed",None)
        for key in ("diagnostics","stop_reason","warnings","search_plan"):
            data.pop(key,None)
        return {**result,"data":data}

    def _confirm(self,state):
        proposal, self.pending = self.pending, None
        if proposal["state"] != state:
            return (yield from self._finish("Предложение устарело: контекст или данные изменились. Сформулируйте действие заново.",[]))
        name, arguments = proposal["name"], proposal["arguments"]
        result = yield from self.dispatcher.events(name,arguments,expected_state=state,grant=canonical(name,arguments))
        call_id = "confirmation_" + uuid4().hex
        self.history.append([
            {"role":"assistant","content":None,"tool_calls":[{"id":call_id,"type":"function","function":{"name":name,"arguments":json.dumps(arguments,ensure_ascii=False)}}]},
            {"role":"tool","tool_call_id":call_id,"content":json.dumps(self._model_result(name,result),ensure_ascii=False)},
        ],state["active_context_id"])
        if result["status"] == "success":
            answer = (result["data"]["summary"] if name == "search_vacancies" else
                      completed(name,self._describe(name,result["data"].get("updated_fields",arguments))))
        else:
            answer = "Действие не выполнено. " + result["error"]["message"]
        return (yield from self._finish(answer,[{"name":name,"result":result}]))

    def chat(self, text):
        return collect(self.events(text))

    def events(self, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Message must be a nonempty string")
        self._llm_calls = 0
        self._confirmed_proposal = None
        state = self.manager.get_state()
        prompt = (self.settings.prompts_dir / "dialogue.md").read_text(encoding="utf-8")
        transcript = self.history.dialogue_messages()
        self.history.append([{"role":"user", "content":text}], state["active_context_id"])
        if self.pending and short_confirmation(text):
            return (yield from self._confirm(state))
        notices, errors, results = [], [], []
        try:
            grants = self._intent(text, state, prompt)
            if self._explain_search:
                report = self.dispatcher.search_diagnostics(state)
                answer = (search_explanation(report) if report else "В активном контексте ещё нет сохранённого поиска, причины которого можно разобрать.")
                return (yield from self._finish(answer,[]))
            if self._confirmed_proposal:
                self.pending = self._confirmed_proposal
                return (yield from self._confirm(state))
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
                           "field_purposes":FIELD_PURPOSES,
                           "authorized_actions":[json.loads(key) for key in sorted(grants)],
                           "unavailable_tools":sorted(set(DEFINITIONS) - set(self.dispatcher.registry.handlers))}
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
                        result = yield from self.dispatcher.events(name, arguments, expected_state=state,
                                                                  grant=signature if signature in grants else None,request_text=text)
                        grants.discard(signature)
                        seen[call_id] = (signature, result)
                        if result["status"] == "success":
                            errors = [item for item in errors if item[0] != name]
                            if name not in READ_ONLY:
                                if name == "search_vacancies":
                                    notices.append(result["data"]["summary"])
                                else:
                                    notices.append(completed(name,self._describe(name, result["data"].get("updated_fields", arguments))))
                                self.pending = None
                            state = self.manager.get_state()
                        elif result["error"]["code"] == "CONFIRMATION_REQUIRED":
                            # Validate before storing the proposal; bind it to the
                            # exact current context/revisions, not just a tool name.
                            validate(name, arguments)
                            self.pending = {"name":name, "arguments":arguments, "state":state}
                        else:
                            errors.append((name,result["error"]["message"]))
                    tool_message = {"role":"tool", "tool_call_id":call_id,
                                    "content":json.dumps(self._model_result(name,result), ensure_ascii=False)}
                    messages.append(tool_message)
                    stored.append(tool_message)
                    results.append({"name":name, "result":result})
                self.history.append(stored, group_context)
            else:
                answer = "Достигнут лимит команд. Продолжите отдельным сообщением."
            if errors:
                answer = " ".join(dict.fromkeys(message for name,message in errors))
            if not self.pending and not errors and count < self.settings.values["dialogue"]["max_tool_calls"]:
                self._capture_proposal(answer, state, prompt)
            if self.pending:
                answer = "Предлагаю: " + self._describe(self.pending["name"], self.pending["arguments"]) + "\nПодтверждаете?"
            # SearchService already presents the evaluated selection. A second
            # free-form listing from the dialogue model duplicates vacancies.
            searched = any(item["name"] == "search_vacancies" and item["result"]["status"] == "success" for item in results)
            answer = "\n".join(notices + ([answer] if not searched or errors or self.pending else []))
        except LLMError as exc:
            answer = "\n".join(notices + [str(exc)])
            self.pending = None
        return (yield from self._finish(answer,results))
