"""HTTP -> dialogue -> source/analyzer -> SQLite -> streamed answer."""

import json
import tempfile
import unittest
from pathlib import Path

from jobradar.config import load_settings
from jobradar.search.service import SearchService
from jobradar.storage import Database
from jobradar.web.app import create_app

from test_dialogue import FakeClient, answer, calls, intent
from test_search import FixtureLLM, FixtureSource, raw

ROOT = Path(__file__).resolve().parents[2]


class CombinedClient(FixtureLLM):
    def __init__(self):
        super().__init__()
        self.dialogue = FakeClient()

    def complete(self,messages,**kwargs):
        try:
            data = json.loads(messages[1]["content"]) if len(messages) > 1 else {}
        except (ValueError,TypeError):
            data = {}
        if "input" in data:
            return super().complete(messages,**kwargs)
        return self.dialogue.complete(messages,**kwargs)


class StreamingSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name)/"stream.sqlite3")
        self.db.initialize()
        self.settings = load_settings(ROOT/"config/v0.1.json")
        self.llm = CombinedClient()
        self.app = create_app(self.db,self.settings,secret_key="test",client_factory=lambda:self.llm)
        self.client = self.app.test_client()
        self.token = self.client.get("/api/state").get_json()["csrf_token"]
        self.orchestrator = next(iter(self.app.extensions["jobradar_orchestrators"].values()))
        self.manager = self.orchestrator.agent.manager
        self.manager.create_profile("Аналитик",authorized=True)
        self.source = FixtureSource([raw()])
        registry = self.orchestrator.agent.dispatcher.registry
        for name in ("search_vacancies","get_search_results","get_vacancy_details"):
            registry.handlers.pop(name)
        SearchService(self.db,self.settings,self.llm,{"hh":self.source}).register(registry)

    def tearDown(self):
        self.temp.cleanup()

    def request(self):
        self.llm.dialogue.responses += [intent([("search_vacancies",{})],evidence="Найди"),
                                       calls(("search1","search_vacancies",{})),answer("Поиск завершён.")]
        return self.client.post("/api/chat",json={"message":"Найди одну вакансию"},
            headers={"X-JobRadar-CSRF":self.token,"Accept":"application/x-ndjson"},buffered=False)

    def test_started_event_arrives_before_planning_and_final_contains_real_results(self):
        response = self.request()
        iterator = iter(response.response)
        accepted = json.loads(next(iterator))
        started = json.loads(next(iterator))
        self.assertEqual(accepted["event"],"accepted")
        self.assertEqual(started["event"],"search_started")
        self.assertEqual(self.llm.calls,[])  # No planner/source/analyzer work before status.
        tail = [json.loads(line) for line in iterator]
        self.assertIn("search_progress",[event["event"] for event in tail])
        self.assertEqual(tail[-1]["event"],"answer")
        self.assertIn("https://hh.ru/vacancy/100",tail[-1]["data"]["answer"])
        self.assertNotIn("calls",tail[-1]["data"])
        response.close()
        with self.db.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM vacancy_cards").fetchone()[0],1)

    def test_connection_close_finishes_task_and_releases_chat_lock(self):
        response = self.request(); iterator = iter(response.response)
        next(iterator); next(iterator)
        response.close()
        with self.db.connection() as db:
            task = db.execute("SELECT state,stop_reason FROM search_tasks").fetchone()
            self.assertEqual(task["state"],"failed")
            self.assertIn("INTERRUPTED",task["stop_reason"])
        self.assertEqual(self.client.get("/api/state").status_code,200)

    def test_explain_errors_uses_saved_diagnostics_without_proposing_search(self):
        response = self.request(); response.get_data(); response.close()
        before = len(self.source.requests)
        self.llm.dialogue.responses += [intent(explain_search=True)]
        response = self.client.post("/api/chat",json={"message":"Почему так много не проанализировано?"},headers={"X-JobRadar-CSRF":self.token})
        answer_text = response.get_json()["answer"]
        self.assertIn("панели диагностики",answer_text)
        self.assertNotIn("Попыток нового анализа",answer_text)
        self.assertNotIn("Подтверждаете",answer_text)
        self.assertEqual(len(self.source.requests),before)
        self.assertEqual(self.llm.dialogue.responses,[])
        with self.db.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM search_tasks").fetchone()[0],1)

    def test_diagnostics_read_is_scoped_to_active_context(self):
        response = self.request(); response.get_data(); response.close()
        report = self.client.get("/api/search-diagnostics").get_json()
        self.assertEqual(report["diagnostics"]["metrics"]["analysis_succeeded"],1)
        self.assertEqual(self.client.get("/api/search-diagnostics?task_id=invalid").status_code,400)
        self.manager.create_context("Другой",authorized=True)
        self.assertEqual(self.client.get(f"/api/search-diagnostics?task_id={report['task_id']}").status_code,403)
        self.assertIsNone(self.client.get("/api/search-diagnostics").get_json())

    def test_diagnostics_can_be_read_while_chat_lock_is_held_and_do_not_leak_into_state(self):
        import threading
        response=self.request();iterator=iter(response.response)
        next(iterator);next(iterator)
        cookie=self.client.get_cookie("jobradar_v01")
        result=[]
        def read():
            client=self.app.test_client();client.set_cookie("jobradar_v01",cookie.value)
            result.append(client.get("/api/search-diagnostics").get_json())
        thread=threading.Thread(target=read,daemon=True);thread.start();thread.join(2)
        try:
            self.assertFalse(thread.is_alive(),"Diagnostics must not wait for the chat lock")
            self.assertEqual(result[0]["status"],"running")
        finally:
            response.close();thread.join(2)
        self.assertNotIn("search_diagnostics",self.client.get("/api/state").get_json())

    def test_task_picker_is_scoped_and_dialogue_model_does_not_receive_diagnostics(self):
        response=self.request();response.get_data();response.close()
        listed=self.client.get("/api/search-diagnostics/tasks").get_json()
        self.assertEqual([row["id"] for row in listed["tasks"]],[1])
        tool_messages=[json.loads(m["content"]) for request,kw in self.llm.dialogue.requests for m in request if m.get("role")=="tool"]
        self.assertTrue(tool_messages)
        for item in tool_messages:
            for key in ("diagnostics","diagnostic_metrics","diagnostic_reasons","stop_reason","unassessed"):
                self.assertNotIn(key,item.get("data",{}))
        self.manager.create_context("Другой",authorized=True)
        self.assertEqual(self.client.get("/api/search-diagnostics/tasks").get_json()["tasks"],[])

    def test_acceptance_two_profiles_contexts_feedback_search_and_restart(self):
        from jobradar.sources.base import RawVacancy

        def turn(text,actions):
            self.llm.dialogue.responses += [intent(actions,evidence=text),calls(*[(f"c{i}",name,args) for i,(name,args) in enumerate(actions)]),answer("Готово")]
            response = self.client.post("/api/chat",json={"message":text},
                headers={"X-JobRadar-CSRF":self.token,"Accept":"application/x-ndjson"})
            events = [json.loads(line) for line in response.get_data().decode().splitlines()]
            self.assertEqual(events[-1]["event"],"answer")
            self.assertNotIn("error",events[-1]["data"])
            response.close()
            return events[-1]["data"]["answer"]

        turn("Создай отдельный контекст для удалённой работы",[("create_context",{"name":"Удалённо","criteria":{"work_formats":["remote"]}})])
        analyst_context = self.manager.get_state()["active_context_id"]
        result = turn("Изучи одну вакансию аналитика",[("search_vacancies",{})])
        self.assertIn("https://hh.ru/vacancy/100",result)
        turn("Учти, что я предпочитаю EdTech",[("update_context",{"changes":{"notes":"Предпочитаю EdTech"}})])
        turn("Повтори поиск",[("search_vacancies",{})])
        self.assertEqual(self.llm.count("extract_and_assess"),1)
        self.assertEqual(self.llm.count("assessment_only"),1)
        turn("Вернись в общий диалог",[("return_to_general",{})])
        turn("Создай профиль водителя с категорией B",[("create_profile",{"profession":"Водитель","profile_data":{"qualifications":[{"type":"driving_license","name":"Удостоверение","details":["B"]}]}})])
        self.assertEqual(self.manager.get_state()["profile"]["profile_data"]["profession"],"Водитель")
        self.source.items = [RawVacancy("hh","200","https://hh.ru/vacancy/200","Водитель","Компания","Требуется категория B.")]
        self.llm.format = "office"
        result = turn("Изучи одну вакансию водителя",[("search_vacancies",{})])
        self.assertIn("https://hh.ru/vacancy/200",result)
        turn("Вернись в мой контекст аналитика",[("switch_context",{"context_id":analyst_context})])
        self.assertEqual(self.manager.get_state()["context"]["criteria"]["notes"],"Предпочитаю EdTech")
        cookie = self.client.get_cookie("jobradar_v01")
        replacement = create_app(self.db,self.settings,secret_key="test",client_factory=CombinedClient).test_client()
        replacement.set_cookie("jobradar_v01",cookie.value)
        restored = replacement.get("/api/state").get_json()
        self.assertEqual(len(restored["profiles"]),2)
        self.assertEqual(restored["state"]["active_context_id"],analyst_context)
        self.assertIn("Учти, что я предпочитаю EdTech",[m["content"] for m in restored["history"]["messages"]])


if __name__ == "__main__":
    unittest.main()
