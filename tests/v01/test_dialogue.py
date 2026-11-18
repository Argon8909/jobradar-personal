"""Scripted DeepSeek responses exercise real services/SQLite without network calls."""

import json
import tempfile
import unittest
from pathlib import Path

from jobradar.config import load_settings
from jobradar.dialogue.agent import DialogueAgent
from jobradar.llm.client import LLMError
from jobradar.orchestration.service import Orchestrator
from jobradar.profiles import ProfileManager
from jobradar.storage import Database
from jobradar.tools.dispatcher import ToolDispatcher, canonical
from jobradar.tools.registry import definitions

ROOT = Path(__file__).resolve().parents[2]


def intent(actions=(), *, accept=False, same=True, evidence="создай", explain_search=False):
    return {"role":"assistant", "content":json.dumps({"actions":[{"name":n,"arguments":a,"evidence":evidence} for n,a in actions],
                                                      "accept_proposal":accept,"same_topic":same,"explain_search":explain_search})}


def calls(*items):
    return {"role":"assistant", "content":None, "tool_calls":[
        {"id":identifier,"type":"function","function":{"name":name,"arguments":json.dumps(arguments)}}
        for identifier, name, arguments in items]}


def answer(text="Готово"):
    return {"role":"assistant", "content":text}


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.proposal = None

    def complete(self, messages, **kwargs):
        self.requests.append((messages,kwargs))
        if kwargs.get("json_mode") and json.loads(messages[-1]["content"]).get("task") == "extract_proposal":
            return answer(json.dumps({"proposal":self.proposal}))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class DialogueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name)/"chat.db")
        self.db.initialize()
        self.manager = ProfileManager.start_session(self.db)
        self.settings = load_settings(ROOT/"config/v0.1.json")

    def tearDown(self):
        self.temp.cleanup()

    def agent(self, *responses):
        return DialogueAgent(self.manager, FakeClient(*responses), self.settings)

    def test_explicit_command_creates_without_second_confirmation_and_updates_tools(self):
        args = {"profession":"Аналитик"}
        agent = self.agent(intent([("create_profile",args)]),calls(("c1","create_profile",args)),answer())
        result = agent.chat("создай профиль аналитика")
        self.assertIsNotNone(result["active_context_id"])
        self.assertIn("Выполнено", result["answer"])
        last_tools = [r[1]["tools"] for r in agent.client.requests if "tools" in r[1]][-1]
        names = {d["function"]["name"] for d in last_tools}
        self.assertNotIn("create_profile", names)
        self.assertIn("update_profile", names)
        stored = agent.history.read()
        self.assertEqual([m["role"] for m in stored], ["user","assistant","tool","assistant"])
        self.assertEqual(stored[2]["tool_call_id"], "c1")

    def test_proposal_requires_yes_and_survives_readonly_call(self):
        args = {"profession":"Аналитик"}
        agent = self.agent(intent(),calls(("c1","create_profile",args)),
                           calls(("c2","list_profiles",{})),answer("Профиль создан"))
        result = agent.chat("Какие есть варианты?")
        self.assertIn("Подтверждаете", result["answer"])
        self.assertEqual(self.manager.list_profiles(), {"profiles":[]})
        agent.client.responses += [intent(accept=True),calls(("c3","create_profile",args)),answer()]
        result = agent.chat("Да, согласен")
        self.assertIsNotNone(result["active_context_id"])

    def test_plain_text_proposal_can_be_accepted_without_repeat_confirmation(self):
        args = {"profession":"Аналитик"}
        agent = self.agent(intent(),answer("Создать профиль аналитика?"))
        agent.client.proposal = {"name":"create_profile","arguments":args,"evidence":"Создать профиль аналитика?"}
        agent.chat("Что посоветуешь?")
        agent.client.proposal = None
        agent.client.responses += [intent(accept=True),calls(("c1","create_profile",args)),answer()]
        result = agent.chat("Да")
        self.assertIsNotNone(result["active_context_id"])
        self.assertNotIn("Подтверждаете", result["answer"])

    def test_topic_change_invalidates_old_confirmation(self):
        args = {"profession":"Аналитик"}
        agent = self.agent(intent(),calls(("c1","create_profile",args)),answer())
        agent.chat("Посоветуй направление")
        agent.client.responses += [intent(same=False),answer("Вернёмся к трудоустройству")]
        agent.chat("Сколько градусов на улице?")
        self.assertIsNone(agent.pending)
        agent.client.responses += [intent(accept=True),calls(("c2","create_profile",args)),answer()]
        agent.chat("Да")
        self.assertEqual(self.manager.list_profiles(), {"profiles":[]})

    def test_confirmation_executes_original_arguments_without_regeneration(self):
        original = {"profession":"Аналитик"}
        different = {"profession":"Водитель"}
        agent = self.agent(intent(),calls(("c1","create_profile",original)),answer())
        agent.chat("Посоветуй направление")
        agent.client.responses += [intent(accept=True),calls(("c2","create_profile",different)),answer()]
        result = agent.chat("Да")
        self.assertEqual(self.manager.list_profiles()["profiles"][0]["profession"],"Аналитик")
        self.assertEqual(result["llm_calls"],0)
        self.assertNotIn("Подтверждаете", result["answer"])

    def test_short_yes_variants_execute_saved_action_without_any_llm_call(self):
        variants = ("да"," Да! ","подтверждаю","я подтверждаю","да,я подтверждаю","Да, согласен")
        for text in variants:
            with self.subTest(text=text):
                agent = self.agent()
                agent.pending = {"name":"create_profile","arguments":{"profession":"Аналитик"},"state":self.manager.get_state()}
                result = agent.chat(text)
                self.assertIsNotNone(result["active_context_id"])
                self.assertEqual(result["llm_calls"],0)
                self.assertEqual(agent.client.requests,[])
                self.manager.return_to_general(authorized=True)

    def test_semantic_consent_also_executes_exact_proposal_after_one_intent_call(self):
        agent = self.agent(intent(accept=True))
        agent.pending = {"name":"create_profile","arguments":{"profession":"Аналитик"},"state":self.manager.get_state()}
        result = agent.chat("Согласен с этим предложением")
        self.assertIsNotNone(result["active_context_id"])
        self.assertEqual(result["llm_calls"],1)

    def test_conditional_yes_does_not_execute_old_proposal(self):
        original = {"profession":"Аналитик"}
        changed = {"profession":"Аналитик","profile_data":{"competencies":["SQL"]}}
        agent = self.agent(intent(),calls(("c2","create_profile",changed)),answer())
        agent.pending = {"name":"create_profile","arguments":original,"state":self.manager.get_state()}
        result = agent.chat("Да, но добавь SQL")
        self.assertEqual(self.manager.list_profiles(),{"profiles":[]})
        self.assertIn("Подтверждаете",result["answer"])

    def test_stale_yes_does_not_call_model_or_change_another_context(self):
        self.manager.create_profile("Аналитик",authorized=True)
        agent = self.agent()
        agent.pending = {"name":"update_context","arguments":{"changes":{"notes":"Старое условие"}},"state":self.manager.get_state()}
        self.manager.create_context("Другой",authorized=True)
        result = agent.chat("Да")
        self.assertIn("устарело",result["answer"])
        self.assertEqual(result["llm_calls"],0)
        self.assertNotIn("notes",self.manager.get_state()["context"]["criteria"])

    def test_context_change_invalidates_pending_consent(self):
        self.manager.create_profile("Аналитик", authorized=True)
        args = {"changes":{"notes":"Новые условия"}}
        agent = self.agent(intent(),calls(("c1","update_context",args)),answer())
        agent.chat("Что посоветуешь?")
        self.manager.create_context("Другой", authorized=True)
        agent.client.responses += [intent(accept=True),calls(("c2","update_context",args)),answer()]
        agent.chat("Да")
        self.assertNotIn("notes", self.manager.get_state()["context"]["criteria"])

    def test_same_batch_calls_recheck_mode_after_transition(self):
        actions = [("create_profile",{"profession":"Аналитик"}),
                   ("update_context",{"changes":{"work_formats":["remote"]}})]
        agent = self.agent(intent(actions),calls(("c1",*actions[0]),("c2",*actions[1]),
                                                ("c3","create_profile",{"profession":"Водитель"})),answer())
        result = agent.chat("создай профиль и поставь удалённую работу")
        self.assertEqual(result["calls"][1]["result"]["status"], "success")
        self.assertEqual(result["calls"][2]["result"]["error"]["code"], "TOOL_NOT_ALLOWED")
        self.assertEqual(self.manager.get_state()["context"]["criteria"]["work_formats"], ["remote"])

    def test_duplicate_tool_call_id_is_not_reexecuted(self):
        self.manager.create_profile("Аналитик", authorized=True)
        args = {"changes":{"notes":"Новые факты"}}
        agent = self.agent(intent([("update_profile",args)]),calls(("c1","update_profile",args)),
                           calls(("c1","update_profile",args)),answer())
        result = agent.chat("создай заметку: новые факты")
        self.assertEqual(self.manager.get_state()["profile"]["revision"], 2)
        self.assertEqual([c["result"]["status"] for c in result["calls"]], ["success","success"])

    def test_invalid_intent_or_fabricated_evidence_has_no_effect(self):
        for response in (answer("не JSON"),intent([("create_profile",{"profession":"A"})],evidence="выдуманная цитата")):
            agent = self.agent(response)
            result = agent.chat("создай профиль")
            self.assertIn("Уточните", result["answer"])
            self.assertEqual(self.manager.list_profiles(), {"profiles":[]})

    def test_llm_error_after_mutation_preserves_data_and_reports_success(self):
        args = {"profession":"Аналитик"}
        agent = self.agent(intent([("create_profile",args)]),calls(("c1","create_profile",args)),LLMError())
        result = agent.chat("создай профиль")
        self.assertIn("Выполнено", result["answer"])
        self.assertEqual(len(self.manager.list_profiles()["profiles"]), 1)

    def test_tool_loop_has_strict_limit(self):
        agent = self.agent(intent(),*[calls((f"c{i}","list_profiles",{})) for i in range(20)])
        result = agent.chat("Покажи профили")
        self.assertEqual(len(result["calls"]), 20)
        self.assertIn("лимит", result["answer"])

    def test_restart_keeps_history_but_not_unconfirmed_permission(self):
        agent = self.agent(intent(),answer("Здравствуйте"))
        agent.chat("Привет")
        replacement = Orchestrator(self.db, self.settings, session_id=self.manager.session_id,
                                   client=FakeClient(intent(),answer("Помню диалог")))
        replacement.chat("Продолжим")
        history = replacement.agent.client.requests[1][0]
        self.assertTrue(any(m.get("content") == "Здравствуйте" for m in history))
        self.assertIsNone(replacement.agent.pending)

    def test_search_unavailable_is_reported_honestly(self):
        self.manager.create_profile("Аналитик", authorized=True)
        agent = self.agent(intent([("search_vacancies",{})]),calls(("c1","search_vacancies",{})),answer("Нашёл 20 вакансий"))
        result = agent.chat("создай поиск")
        self.assertIn("ещё не подключены", result["answer"])
        self.assertNotIn("Нашёл 20", result["answer"])

    def test_dispatcher_rejects_foreign_context_and_stale_state(self):
        state = self.manager.get_state()
        with self.db.transaction() as db:
            db.execute("INSERT INTO users(id) VALUES (2)")
        other = ProfileManager.start_session(self.db, 2)
        target = other.create_profile("Другой", authorized=True)["initial_context_id"]
        dispatcher = ToolDispatcher(self.manager)
        args = {"context_id":target}
        denied = dispatcher.execute("switch_context",args,expected_state=state,grant=canonical("switch_context",args))
        self.assertEqual(denied["error"]["code"], "ACCESS_DENIED")
        self.manager.create_profile("Аналитик", authorized=True)
        stale = dispatcher.execute("list_profiles",{},expected_state=state)
        self.assertEqual(stale["error"]["code"], "REVISION_CONFLICT")

    def test_dispatcher_rejects_malformed_input_and_does_not_trust_grant_field(self):
        dispatcher = ToolDispatcher(self.manager)
        state = self.manager.get_state()
        result = dispatcher.execute("create_profile",{"profession":"A","authorized":True},expected_state=state)
        self.assertEqual(result["error"]["code"], "VALIDATION_ERROR")
        result = dispatcher.execute("create_profile",{"profession":"A"},expected_state=state)
        self.assertEqual(result["error"]["code"], "CONFIRMATION_REQUIRED")

    def test_llm_budget_and_orphan_history_are_safe(self):
        from dataclasses import replace

        settings = replace(self.settings, values={**self.settings.values,
                           "dialogue":{"max_tool_calls":20,"max_llm_calls":2}})
        agent = DialogueAgent(self.manager, FakeClient(intent(),calls(("c1","list_profiles",{}))), settings)
        agent.history.append([calls(("old","list_profiles",{}))], None)
        result = agent.chat("Покажи профили")
        self.assertIn("лимит обращений", result["answer"])
        self.assertEqual(result["llm_calls"], 2)
        self.assertTrue(all(m["role"] != "tool" and not m.get("tool_calls")
                            for m in agent.client.requests[1][0]))

    def test_database_error_does_not_leak_sql_or_secret(self):
        import sqlite3
        from unittest.mock import patch

        dispatcher = ToolDispatcher(self.manager)
        state = self.manager.get_state()
        args = {"profession":"A"}
        with patch("jobradar.profiles.manager.repository.create_profile",
                   side_effect=sqlite3.OperationalError("SELECT private-secret")):
            result = dispatcher.execute("create_profile",args,expected_state=state,
                                        grant=canonical("create_profile",args))
        self.assertEqual(result["error"]["code"], "DB_ERROR")
        self.assertNotIn("private-secret", json.dumps(result))
        self.assertEqual(self.manager.list_profiles(), {"profiles":[]})

    def test_context_guard_checks_inside_mutating_transaction(self):
        first = self.manager.create_profile("Аналитик", authorized=True)
        guarded = ProfileManager(self.db, 1, self.manager.session_id,
                                 expected_context_id=first["initial_context_id"])
        self.manager.create_context("Другой", authorized=True)
        from jobradar.profiles import ProfileError
        with self.assertRaises(ProfileError) as result:
            guarded.update_profile({"notes":"Нельзя"}, authorized=True)
        self.assertEqual(result.exception.code, "REVISION_CONFLICT")


if __name__ == "__main__":
    unittest.main()
