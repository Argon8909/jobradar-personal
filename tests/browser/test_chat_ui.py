"""Run explicitly with an installed Playwright Chromium: unittest discover -s tests/browser."""

import os
import sys
import tempfile
import threading
import unittest
import json
import time
from contextlib import contextmanager
from unittest.mock import patch
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

    def test_readable_confirmation_accepts_plain_yes_without_model_regeneration(self):
        args = {"profession":"Системный аналитик","profile_data":{"competencies":["SQL","REST API"],"experience":[{"domain":"API","years":1.42}]}}
        self.fake.responses += [intent(),calls(("p1","create_profile",args)),answer("Создать профиль?")]
        self.page.locator("#message").fill("Обсудим мою профессию")
        self.page.get_by_role("button",name="Отправить сообщение").click()
        last = self.page.locator(".assistant .message-content").last
        expect(last).to_contain_text("Подтверждаете?")
        expect(last).to_contain_text("Навыки: SQL; REST API")
        expect(last).not_to_contain_text("profile_data")
        expect(last).not_to_contain_text("{")
        self.fake.responses.clear()
        before = len(self.fake.requests)
        self.page.locator("#message").fill("да")
        self.page.get_by_role("button",name="Отправить сообщение").click()
        expect(self.page.locator("#context-title")).to_have_text("Системный аналитик")
        expect(self.page.locator(".assistant .message-content").last).to_contain_text("Создан профиль")
        expect(self.page.locator(".assistant .message-content").last).not_to_contain_text("Подтверждаете?")
        self.assertEqual(len(self.fake.requests),before)

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

    def test_hh_jsonld_and_delayed_description_fallback(self):
        from jobradar.sources.hh import extract, HHAdapter
        adapter = HHAdapter(load_settings(ROOT/"config/v0.1.json"))
        self.page.set_content('<h1>Аналитик</h1><script type="application/ld+json">{"@type":"WebSite"}</script>')
        self.page.evaluate("setTimeout(() => {const el=document.createElement('div'); el.className='vacancy-description'; el.innerText='SQL\\nSQL\\nREST API'; document.body.append(el);}, 150)")
        adapter._wait_content(self.page,time.monotonic()+5)
        self.assertEqual(extract(self.page,"1").description.count("SQL"),2)
        payload = {"@context":"https://schema.org","@graph":[{"@type":"JobPosting","title":"Системный аналитик",
                   "hiringOrganization":{"name":"Компания"},"description":"<p>SQL</p><p>REST API &amp; JSON</p>"}]}
        self.page.set_content('<script type="application/ld+json">'+json.dumps(payload)+'</script>')
        adapter._wait_content(self.page,time.monotonic()+5)
        vacancy = extract(self.page,"2")
        self.assertEqual(vacancy.title,"Системный аналитик")
        self.assertIn("REST API & JSON",vacancy.description)

    def test_publication_date_jsonld_offsets_fallbacks_and_diagnostics_panel(self):
        from jobradar.sources.hh import extract
        from jobradar.profiles import ProfileManager
        from jobradar.search.service import SearchService
        from jobradar.events import collect
        from dataclasses import replace
        from datetime import datetime, timezone, timedelta
        from test_search import FixtureLLM, FixtureSource, raw
        template = '<h1>Аналитик</h1><div data-qa="vacancy-description">SQL</div>'
        for date,expected in [('2026-03-01T01:00:00+03:00','2026-02-28T22:00:00+00:00'),
                              ('2026-03-01','2026-02-28T21:00:00+00:00'),('bad',None)]:
            payload={'@graph':[{'@type':['JobPosting'],'datePosted':date}]}
            self.page.set_content(template+'<script type="application/ld+json">'+json.dumps(payload)+'</script>')
            self.assertEqual(extract(self.page,'123').published_at,expected)
        self.page.set_content(template+'<meta itemprop="datePosted" content="2026-03-01T01:00:00Z">')
        self.assertEqual(extract(self.page,'123').published_at,'2026-03-01T01:00:00+00:00')
        self.page.set_content(template+'<time data-qa="vacancy-publication-date" datetime="2026-03-01"></time>')
        self.assertEqual(extract(self.page,'123').published_at,'2026-02-28T21:00:00+00:00')
        self.page.set_content(template+'<meta itemprop="dateModified" content="2026-03-01">')
        self.assertIsNone(extract(self.page,'123').published_at)
        # Same session as the UI; render actual diagnostic data through Flask.
        with self.db.connection() as db:
            session_id=db.execute('select id from user_sessions order by id desc limit 1').fetchone()[0]
        manager=ProfileManager(self.db,1,session_id);manager.create_profile('Аналитик',authorized=True)
        old=(datetime.now(timezone.utc)-timedelta(days=60)).isoformat()
        service=SearchService(self.db,load_settings(ROOT/'config/v0.2.json'),FixtureLLM(),{'hh':FixtureSource([replace(raw('100'),published_at=old),raw('101')])})
        report=collect(service.search(manager,{},manager.get_state()))
        self.page.goto(f'http://127.0.0.1:{self.server.server_port}/diagnostics')
        expect(self.page.locator('#summary')).to_contain_text('30 календарных дней')
        expect(self.page.locator('#summary')).to_contain_text('источник: конфигурация')
        expect(self.page.locator('#summary')).to_contain_text('Исключено по возрасту: 1')
        expect(self.page.locator('#summary')).to_contain_text('Дата публикации неизвестна: 1')

    def test_geography_panel_shows_active_context_proposal_and_applied_regions(self):
        from jobradar.profiles import ProfileManager
        from jobradar.search.service import SearchService
        from jobradar.events import collect
        from test_search import FixtureLLM, FixtureSource, raw
        with self.db.connection() as db:
            session_id=db.execute('select id from user_sessions order by id desc limit 1').fetchone()[0]
        manager=ProfileManager(self.db,1,session_id)
        manager.create_profile('Системный аналитик',authorized=True)
        manager.update_context({'locations':['Россия']},authorized=True)
        client=FixtureLLM();original=client.complete
        def proposal(messages,**kwargs):
            result=original(messages,**kwargs)
            if 'планирования' in messages[0]['content']:
                data=json.loads(result['content']);data['search_queries']['queries'][0]['areas']=['Казахстан']
                result['content']=json.dumps(data)
            return result
        client.complete=proposal
        service=SearchService(self.db,load_settings(ROOT/'config/v0.2.json'),client,{'hh':FixtureSource([raw('1')])})
        report=collect(service.search(manager,{'request':'давай снова поищем вакансии системного аналитика'},manager.get_state()))
        self.page.goto(f'http://127.0.0.1:{self.server.server_port}/diagnostics')
        expect(self.page.locator('#context')).to_contain_text('Системный аналитик')
        expect(self.page.locator('#context')).to_contain_text('сохранённая география: Россия')
        expect(self.page.locator('#summary')).to_contain_text('Казахстан')
        expect(self.page.locator('#plan')).to_contain_text('Россия')
        expect(self.page.locator('#plan')).not_to_contain_text('Казахстан')
        with self.page.expect_download() as info:
            self.page.locator('#export').click()
        payload=json.loads(Path(info.value.path()).read_text())
        self.assertEqual(payload['task_id'],report['task_id'])
        self.assertEqual(payload['active_context']['saved_locations'],['Россия'])
        self.assertEqual(payload['diagnostics']['context_at_start']['context_id'],manager.get_state()['active_context_id'])

    def test_hh_preserves_format_filter_and_deduplicates_across_queries(self):
        from urllib.parse import parse_qs,urlparse
        from jobradar.sources.hh import HHAdapter
        from jobradar.sources.base import RawVacancy,SourceEvent
        adapter=HHAdapter(load_settings(ROOT/"config/v0.1.json"))
        requests=[]
        def site(route):
            query=parse_qs(urlparse(route.request.url).query)
            if "/search/" in route.request.url:
                requests.append(query)
                if query.get("page")==["0"]:
                    ids=["10","11"] if query.get("text")==["first"] else ["11","12"]
                    body="".join(f'<a href="https://hh.ru/vacancy/{vid}">{vid}</a>' for vid in ids)
                else:
                    body='<div data-qa="vacancy-serp__no-vacancies">Empty</div>'
            else:
                body='<h1>Analyst</h1><div data-qa="vacancy-description">SQL</div>'
            route.fulfill(content_type="text/html; charset=utf-8",body=body)
        self.context.route("https://hh.ru/**",site)
        @contextmanager
        def browser_context():
            yield self.context
        seen=set()
        with patch.object(adapter,"_browser",browser_context):
            first=list(adapter.search({"text":"first","areas":["Москва"],"filters":{"work_formats":["remote"]}},limit=2,deadline=time.monotonic()+10,seen=seen))
            second=list(adapter.search({"text":"second","areas":["Москва"],"filters":{"work_formats":["remote"]}},limit=1,deadline=time.monotonic()+10,seen=seen))
        self.assertEqual([v.external_id for v in first+second if isinstance(v,RawVacancy)],["10","11","12"])
        self.assertTrue(any(isinstance(v,SourceEvent) and v.kind=="source_duplicate" for v in second))
        self.assertEqual(len(seen),3)
        self.assertTrue(all(q["area"]==["1"] and q["work_format"]==["REMOTE"] for q in requests))

    def test_partial_selection_is_visible_before_end_and_replaced_without_duplicates(self):
        from jobradar.search.service import SearchService
        from test_search_stream import CombinedClient
        from test_search import FixtureSource,raw
        service=next(iter(self.app.extensions["jobradar_orchestrators"].values()))
        service.agent.manager.create_profile("Аналитик",authorized=True)
        combined=CombinedClient()
        combined.dialogue.responses += [intent([("search_vacancies",{})],evidence="Найди"),calls(("s1","search_vacancies",{})),answer("https://hh.ru/vacancy/100 https://hh.ru/vacancy/100")]
        service.agent.client=combined
        released=threading.Event()
        class PausedSource(FixtureSource):
            def search(inner,query,**kwargs):
                yield from super().search(query,**kwargs)
                released.wait(5)
                yield raw("100")
        registry=service.agent.dispatcher.registry
        for name in ("search_vacancies","get_search_results","get_vacancy_details"):
            registry.handlers.pop(name)
        SearchService(self.db,load_settings(ROOT/"config/v0.1.json"),combined,{"hh":PausedSource([raw(str(i)) for i in range(100,110)])}).register(registry)
        self.page.locator("#message").fill("Найди вакансии")
        self.page.get_by_role("button",name="Отправить сообщение").click()
        try:
            expect(self.page.locator("[data-search-preview]")).to_have_count(1)
            expect(self.page.locator('#messages a[href="https://hh.ru/vacancy/100"]')).to_have_count(1)
        finally:
            released.set()
        expect(self.page.locator("#message")).to_be_enabled()
        expect(self.page.locator("[data-search-preview]")).to_have_count(0)
        expect(self.page.locator('#messages a[href="https://hh.ru/vacancy/100"]')).to_have_count(1)

    def test_hh_manual_challenge_emits_status_and_resumes_after_user_action(self):
        from jobradar.sources.hh import HHAdapter
        from jobradar.events import collect
        adapter = HHAdapter(load_settings(ROOT/"config/v0.1.json"))
        cleared = False
        def site(route):
            route.fulfill(body='<h1>HH fixture</h1>' + ('' if cleared else '<input name="captcha">'),content_type="text/html")
        self.page.route("https://hh.ru/**",site)
        iterator = adapter._navigate(self.page,"https://hh.ru/search/vacancy",time.monotonic()+5)
        event = next(iterator)
        self.assertEqual((event.kind,event.code),("source_action_required","CAPTCHA"))
        # Simulate a person completing a fixture, never solve a real CAPTCHA.
        cleared = True
        self.page.locator('input[name="captcha"]').evaluate("el => el.remove()")
        self.assertEqual(next(iterator).kind,"source_resumed")
        self.assertEqual(collect(iterator).status,200)

    def test_hh_region_is_resolved_from_visible_controls_without_api(self):
        from jobradar.sources.hh import HHAdapter
        from jobradar.events import collect
        adapter = HHAdapter(load_settings(ROOT/"config/v0.1.json"))
        requests = []
        def site(route):
            requests.append(route.request.url)
            route.fulfill(content_type="text/html; charset=utf-8",body='''<button data-qa="advanced-search-region-selectFromList" onclick="document.getElementById('regions').hidden=false">Регион</button>
            <div id="regions" role="dialog" hidden><input type="text" aria-label="Поиск региона"><label><input type="checkbox" value="104">Челябинск</label></div>''')
        self.context.route("https://hh.ru/**",site)
        area = collect(adapter._area(self.context,"Челябинск",time.monotonic()+5))
        self.assertEqual(area,"104")
        self.assertEqual(requests,["https://hh.ru/search/vacancy/advanced"])

    def test_hh_search_continues_after_unreadable_detail_page(self):
        from jobradar.sources.hh import HHAdapter
        from jobradar.sources.base import RawVacancy, SourceEvent
        settings = load_settings(ROOT/"config/v0.1.json")
        settings.values["source"]["content_timeout_seconds"] = 1
        adapter = HHAdapter(settings)
        def site(route):
            path = route.request.url
            body = ('<a href="https://hh.ru/vacancy/10">1</a><a href="https://hh.ru/vacancy/11">2</a>' if "/search/" in path else
                    '<h1>Analyst</h1>' + ('<div data-qa="vacancy-description">SQL and REST API</div>' if path.endswith("/11") else ''))
            route.fulfill(content_type="text/html; charset=utf-8",body=body)
        self.context.route("https://hh.ru/**",site)
        @contextmanager
        def browser_context():
            yield self.context
        with patch.object(adapter,"_browser",browser_context):
            items = list(adapter.search({"text":"analyst"},limit=2,deadline=time.monotonic()+10))
        discovered=[item for item in items if isinstance(item,SourceEvent) and item.kind == "source_discovered"]
        self.assertEqual(len(discovered),2)
        items=[item for item in items if not isinstance(item,SourceEvent) or item.kind != "source_discovered"]
        self.assertEqual(len(items),2)
        self.assertIsInstance(items[0],SourceEvent)
        self.assertEqual(items[0].code,"VACANCY_UNREADABLE")
        self.assertIsInstance(items[1],RawVacancy)
        self.assertEqual(items[1].external_id,"11")

    def test_unassessed_diagnostics_are_separate_and_safe_to_render(self):
        from jobradar.search.service import SearchService
        from test_search import FixtureSource, raw
        from test_search_stream import CombinedClient
        service = next(iter(self.app.extensions["jobradar_orchestrators"].values()))
        service.agent.manager.create_profile("Аналитик",authorized=True)
        combined = CombinedClient()
        combined.dialogue.responses += [intent([("search_vacancies",{})],evidence="Найди"),calls(("s1","search_vacancies",{})),answer("Готово")]
        original = combined.complete
        def invalid(messages,**kwargs):
            response = original(messages,**kwargs)
            try: data = json.loads(messages[1]["content"]).get("input",{})
            except (ValueError,TypeError): data = {}
            if data.get("mode") == "extract_and_assess":
                result = json.loads(response["content"]); result["assessment"]["reasoning"] = 0
                result["card"]["summary"] = '<img src=x onerror="window.__diagnosticXSS=true">'
                response["content"] = json.dumps(result)
            return response
        combined.complete = invalid
        service.agent.client = combined
        registry = service.agent.dispatcher.registry
        for name in ("search_vacancies","get_search_results","get_vacancy_details"):
            registry.handlers.pop(name)
        SearchService(self.db,load_settings(ROOT/"config/v0.1.json"),combined,{"hh":FixtureSource([raw()])}).register(registry)
        self.page.locator("#message").fill("Найди вакансию")
        self.page.get_by_role("button",name="Отправить сообщение").click()
        expect(self.page.locator("#messages")).to_contain_text("найдено 1, оценено 0")
        expect(self.page.locator("#messages")).not_to_contain_text("LLM_SCHEMA_INVALID")
        expect(self.page.locator("#messages")).not_to_contain_text("/assessment/reasoning")
        expect(self.page.get_by_role("link",name="Открыть диагностику поисков")).to_be_visible()
        self.page.goto(f"http://127.0.0.1:{self.server.server_port}/diagnostics")
        expect(self.page.locator("#summary")).to_contain_text("Поиск #1")
        expect(self.page.locator("#issues")).to_contain_text("LLM_SCHEMA_INVALID")
        expect(self.page.locator("#issues")).to_contain_text("/assessment/reasoning")
        expect(self.page.locator("#calls details").filter(has=self.page.locator("summary",has_text="vacancy_analyzer")).first).to_be_visible()
        self.assertEqual(self.page.locator("#calls img").count(),0)
        self.assertFalse(self.page.evaluate("Boolean(window.__diagnosticXSS)"))
        with self.page.expect_download() as download:
            self.page.get_by_role("button",name="Скачать диагностический JSON").click()
        destination=Path(self.temp.name)/"diagnostic-export.json"
        download.value.save_as(destination)
        exported=json.loads(destination.read_text())
        self.assertEqual(exported["task_id"],1)
        self.assertTrue(any(call.get("response") for call in exported["diagnostics"]["llm_calls"]))



if __name__ == "__main__":
    unittest.main()
