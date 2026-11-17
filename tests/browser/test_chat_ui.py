"""Run explicitly with an installed Playwright Chromium: unittest discover -s tests/browser."""

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server

from jobradar.config import load_settings
from jobradar.storage import Database
from jobradar.web.app import create_app

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests/v01"))
from test_dialogue import FakeClient, answer, calls, intent


class BrowserChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db = Database(Path(self.temp.name)/"browser.sqlite3")
        db.initialize()
        self.fake = FakeClient()
        app = create_app(db, load_settings(ROOT/"config/v0.1.json"),secret_key="browser-test",client_factory=lambda:self.fake)
        self.app, self.db = app, db
        self.server = make_server("127.0.0.1",0,app,threaded=True)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.context = self.browser.new_context(viewport={"width":1440,"height":960})
        self.page = self.context.new_page()
        self.errors = []
        self.page.on("pageerror",lambda error:self.errors.append(str(error)))
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}")
        expect(self.page.locator("#empty")).to_be_visible()
        expect(self.page.locator("#message")).to_be_enabled()

    def tearDown(self):
        self.context.close()
        self.server.shutdown()
        self.thread.join(3)
        self.server.server_close()
        self.temp.cleanup()
        self.assertEqual(self.errors,[])

    def screenshot(self,name):
        destination = os.environ.get("JOBRADAR_SCREENSHOTS")
        if destination:
            root = Path(destination)
            root.mkdir(parents=True,exist_ok=True)
            self.page.screenshot(path=str(root/name),full_page=True)

    def test_create_profile_and_refresh_restore_state(self):
        self.screenshot("desktop-empty.png")
        args = {"profession":"Аналитик"}
        self.fake.responses += [intent([("create_profile",args)]),calls(("c1","create_profile",args)),answer("Профиль готов.")]
        self.page.locator("#message").fill("создай профиль аналитика")
        self.page.get_by_role("button",name="Отправить сообщение").click()
        expect(self.page.locator("#context-title")).to_have_text("Аналитик")
        expect(self.page.locator("#messages .message")).to_have_count(2)
        expect(self.page.locator("#profiles .profile-name")).to_have_text("Аналитик")
        self.page.reload()
        expect(self.page.locator("#context-title")).to_have_text("Аналитик")
        expect(self.page.locator("#messages .message")).to_have_count(2)
        self.screenshot("desktop-chat.png")

    def test_mobile_keyboard_and_untrusted_content(self):
        self.page.set_viewport_size({"width":390,"height":844})
        self.page.get_by_role("button",name="Обсудить направление").click()
        expect(self.page.locator("#message")).to_have_value("Помоги разобраться, какая работа мне подойдёт")
        self.page.locator("#message").fill("Вопрос")
        self.page.locator("#message").press("Shift+Enter")
        self.page.locator("#message").press("End")
        self.page.locator("#message").type("вторая строка")
        expect(self.page.locator("#message")).to_have_value("Вопрос\nвторая строка")
        text = '<img src=x onerror="window.__xss=true">'
        self.fake.responses += [intent(),answer(text)]
        self.page.locator("#message").press("Enter")
        expect(self.page.locator(".assistant .message-content")).to_have_text(text)
        self.assertEqual(self.page.locator("#messages img").count(),0)
        self.assertFalse(self.page.evaluate("Boolean(window.__xss)"))
        self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        self.screenshot("mobile-chat.png")

    def test_network_failure_preserves_input_and_offers_history_refresh(self):
        self.page.route("**/api/chat",lambda route:route.abort())
        self.page.locator("#message").fill("Сообщение")
        self.page.get_by_role("button",name="Отправить сообщение").click()
        expect(self.page.locator("#error")).to_be_visible()
        expect(self.page.locator("#message")).to_have_value("Сообщение")
        expect(self.page.locator("#message")).to_be_enabled()
        self.assertEqual(self.fake.requests,[])
        self.page.get_by_role("button",name="Обновить историю").click()
        expect(self.page.locator("#error")).to_be_hidden()

    def test_hh_parser_uses_full_description_and_safe_vacancy_links(self):
        from jobradar.sources.hh import extract, vacancy_links, challenge

        self.page.set_content('''<h1 data-qa="vacancy-title">Аналитик</h1>
          <div data-qa="vacancy-company-name">Компания</div><div data-qa="vacancy-salary">от 200 000 ₽</div>
          <div data-qa="vacancy-description">SQL<br>SQL<br>PostgreSQL</div>
          <a href="https://hh.ru/vacancy/123?from=search">Вакансия</a>
          <a href="https://evilhh.ru/vacancy/666">Чужая ссылка</a>''')
        vacancy = extract(self.page,"123")
        self.assertIn("от 200 000 ₽",vacancy.description)
        self.assertEqual(vacancy.description.count("SQL"),3)
        self.assertEqual(vacancy_links(self.page),["123"])
        self.assertFalse(challenge(self.page))
        self.page.set_content('<input name="captcha">')
        self.assertTrue(challenge(self.page))

    def test_search_status_and_results_arrive_through_stream(self):
        from jobradar.search.service import SearchService
        from test_search_stream import CombinedClient
        from test_search import FixtureSource, raw

        service = next(iter(self.app.extensions["jobradar_orchestrators"].values()))
        service.agent.manager.create_profile("Аналитик",authorized=True)
        combined = CombinedClient()
        combined.dialogue.responses += [intent([("search_vacancies",{})],evidence="Найди"),calls(("s1","search_vacancies",{})),answer("Готово")]
        service.agent.client = combined
        released = threading.Event()
        class DelayedSource(FixtureSource):
            def search(inner,query,**kwargs):
                released.wait(5)
                yield from super().search(query,**kwargs)
        registry = service.agent.dispatcher.registry
        for name in ("search_vacancies","get_search_results","get_vacancy_details"):
            registry.handlers.pop(name)
        SearchService(self.db,load_settings(ROOT/"config/v0.1.json"),combined,{"hh":DelayedSource([raw()])}).register(registry)
        self.page.locator("#message").fill("Найди одну вакансию")
        self.page.get_by_role("button",name="Отправить сообщение").click()
        try:
            expect(self.page.locator("#status")).to_have_text("Поиск запущен")
        finally:
            released.set()
        expect(self.page.locator("#messages")).to_contain_text("найдено 1, оценено 1")
        expect(self.page.locator('#messages a[href="https://hh.ru/vacancy/100"]')).to_be_visible()
        self.screenshot("desktop-search.png")


if __name__ == "__main__":
    unittest.main()
