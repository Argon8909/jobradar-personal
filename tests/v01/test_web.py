"""Flask integration tests use the real orchestrator and SQLite with fake LLM I/O."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from jobradar.config import load_settings
from jobradar.profiles import ProfileManager
from jobradar.storage import Database
from jobradar.web.app import create_app, session_secret

from test_dialogue import FakeClient, answer, calls, intent

ROOT = Path(__file__).resolve().parents[2]


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name)/"web.sqlite3")
        self.db.initialize()
        self.settings = load_settings(ROOT/"config/v0.1.json")
        self.fake = FakeClient()
        self.app = create_app(self.db, self.settings, secret_key="test-secret", client_factory=lambda:self.fake)
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def bootstrap(self):
        return self.client.get("/api/state").get_json()

    def post(self, text, csrf):
        return self.client.post("/api/chat", json={"message":text}, headers={"X-JobRadar-CSRF":csrf})

    def test_home_and_assets_are_packaged_and_no_llm_called(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("JobRadar", response.get_data(as_text=True))
        for path in ("/static/chat.css","/static/chat.js"):
            with self.client.get(path) as asset:
                self.assertEqual(asset.status_code, 200)
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(self.bootstrap()["profiles"], [])

    def test_post_without_token_is_rejected_before_any_side_effect(self):
        response = self.client.post("/api/chat",json={"message":"создай профиль"})
        self.assertEqual(response.status_code, 403)
        with self.db.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM user_sessions").fetchone()[0],0)

    def test_untrusted_host_rejected(self):
        self.assertEqual(self.client.get("/",base_url="http://untrusted.example").status_code,400)

    def test_chat_create_profile_updates_history_and_context(self):
        token = self.bootstrap()["csrf_token"]
        args = {"profession":"Аналитик"}
        self.fake.responses += [intent([("create_profile",args)]),calls(("c1","create_profile",args)),answer()]
        response = self.post("создай профиль аналитика", token)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Выполнено", response.get_json()["answer"])
        state = self.bootstrap()
        self.assertEqual(state["state"]["profession"], "Аналитик")
        self.assertEqual(state["state"]["context_name"], "Основной поиск")
        self.assertEqual([m["role"] for m in state["history"]["messages"]], ["user","assistant"])
        self.assertNotIn("tool_calls", json.dumps(state))
        self.assertNotIn("calls", response.get_json())

    def test_cookie_survives_app_restart_and_resumes_context(self):
        data = self.bootstrap()
        args = {"profession":"Аналитик"}
        self.fake.responses += [intent([("create_profile",args)]),calls(("c1","create_profile",args)),answer()]
        self.post("создай профиль", data["csrf_token"])
        cookie = self.client.get_cookie("jobradar_v01")
        self.assertIsNotNone(cookie.expires)
        replacement = create_app(self.db, self.settings, secret_key="test-secret", client_factory=lambda:FakeClient())
        other = replacement.test_client()
        other.set_cookie("jobradar_v01", cookie.value)
        restored = other.get("/api/state").get_json()
        self.assertEqual(restored["state"]["profession"], "Аналитик")
        self.assertEqual(restored["csrf_token"], data["csrf_token"])
        self.assertEqual(len(restored["history"]["messages"]), 2)

    def test_csrf_wrong_origin_and_unsigned_cookie_rejected(self):
        token = self.bootstrap()["csrf_token"]
        response = self.client.post("/api/chat",json={"message":"x"},headers={"X-JobRadar-CSRF":token,"Origin":"https://other.example"})
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.post("x", "invalid").status_code,403)
        self.assertEqual(self.post("x", "русский").status_code,403)
        self.client.set_cookie("jobradar_v01","forged")
        self.assertEqual(self.post("x",token).status_code,403)
        self.assertEqual(self.fake.requests,[])

    def test_invalid_input_is_rejected_without_llm_or_messages(self):
        token = self.bootstrap()["csrf_token"]
        headers = {"X-JobRadar-CSRF":token}
        for body in ({"message":" "},{"message":123},{"message":"x","session_id":2},["x"],None):
            with self.subTest(body=body):
                self.assertEqual(self.client.post("/api/chat",json=body,headers=headers).status_code,400)
        self.assertEqual(self.post("x"*60001,token).status_code,400)
        self.assertEqual(self.client.post("/api/chat",data="x"*300000,headers=headers,content_type="application/json").status_code,413)
        self.assertEqual(self.fake.requests,[])

    def test_history_pagination_is_ordered_and_does_not_mix_users(self):
        self.bootstrap()
        service = next(iter(self.app.extensions["jobradar_orchestrators"].values()))
        history = service.agent.history
        for index in range(65):
            history.append([{"role":"user","content":f"Сообщение {index}"}],None)
        with self.db.transaction() as db:
            db.execute("INSERT INTO users(id) VALUES (2)")
        other = ProfileManager.start_session(self.db,2)
        from jobradar.storage.chat import ChatHistory
        ChatHistory(other).append([{"role":"user","content":"Чужое сообщение"}],None)
        first = self.bootstrap()["history"]
        second = self.client.get(f"/api/history?before={first['next_before']}").get_json()
        messages = second["messages"] + first["messages"]
        self.assertEqual([m["content"] for m in messages], [f"Сообщение {i}" for i in range(65)])
        self.assertFalse(second["has_more"])
        for before in ("-1","abc",str(2**64)):
            self.assertEqual(self.client.get("/api/history?before="+before).status_code,400)

    def test_second_tab_gets_busy_instead_of_duplicate_turn(self):
        token = self.bootstrap()["csrf_token"]
        service = next(iter(self.app.extensions["jobradar_orchestrators"].values()))
        acquired, release = threading.Event(), threading.Event()
        def occupy():
            with service.lock:
                acquired.set()
                release.wait(5)
        worker = threading.Thread(target=occupy)
        worker.start()
        self.assertTrue(acquired.wait(2))
        try:
            self.assertEqual(self.post("x",token).status_code,409)
            self.assertEqual(self.fake.requests,[])
        finally:
            release.set(); worker.join(2)

    def test_secret_is_persisted_without_changing_deepseek_key(self):
        path = Path(self.temp.name)/".env"
        path.write_text("DEEPSEEK_API_KEY=test-private\n",encoding="utf-8")
        first = session_secret(path)
        self.assertEqual(session_secret(path), first)
        self.assertIn("DEEPSEEK_API_KEY=test-private",path.read_text())

    def test_internal_failure_returns_error_status_not_empty_success(self):
        from unittest.mock import patch

        token = self.bootstrap()["csrf_token"]
        service = next(iter(self.app.extensions["jobradar_orchestrators"].values()))
        with patch.object(service,"chat",return_value={"answer":"База недоступна","error":{"code":"DB_ERROR","retryable":False}}):
            response = self.post("x",token)
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.get_json()["error"]["message"], "База недоступна")


if __name__ == "__main__":
    unittest.main()
