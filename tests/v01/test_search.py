"""Real SQLite pipeline with deterministic source and LLM boundaries."""

import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path

from jobradar.config import load_settings
from jobradar.events import collect
from jobradar.profiles import ProfileError, ProfileManager
from jobradar.search.budget import TaskBudget
from jobradar.search.service import SearchService
from jobradar.sources.base import RawVacancy, SourceError, SourceEvent
from jobradar.storage import Database, search as tasks, vacancies

ROOT = Path(__file__).resolve().parents[2]


def raw(identifier="100", description="Нужен SQL и PostgreSQL. Работа удалённо."):
    return RawVacancy("hh",identifier,f"https://hh.ru/vacancy/{identifier}","Аналитик SQL","Компания",description)


class FixtureSource:
    name = "hh"

    def __init__(self, items):
        self.items, self.requests, self.closed = items, [], 0

    def search(self, query, *, limit, deadline):
        self.requests.append(query)
        try:
            for item in self.items[:limit]:
                if isinstance(item,Exception):
                    raise item
                yield item
        finally:
            self.closed += 1


class FixtureLLM:
    def __init__(self):
        self.calls = []
        self.target = 1
        self.format = "remote"
        self.malformed_id = None
        self.ambiguous = False
        self.plan_source = "hh"

    def complete(self, messages, **kwargs):
        self.calls.append((messages,kwargs))
        prompt, data = messages[0]["content"], json.loads(messages[1]["content"])["input"]
        if "планирования" in prompt:
            query = "Водитель категория B" if data["profile"]["profession"] == "Водитель" else "Аналитик SQL"
            result = {"search_queries":{"version":1,"queries":[{"source":self.plan_source,"text":query,"filters":{}}]},"target_count":self.target}
        elif "сопоставления" in prompt:
            result = {"decision":"ambiguous" if self.ambiguous else "use_existing",
                      "tag_id":None if self.ambiguous else data["candidates"][0]["id"]}
        else:
            assessment = {"decision":"Подходит","reasoning":"Есть соответствующий опыт SQL.","relevance_score":None}
            if data["profile"]["profession"] == "Водитель":
                assessment["reasoning"] = "Подтверждена категория B."
            if data["mode"] == "assessment_only":
                result = assessment
            else:
                vacancy = data["vacancy"]
                tag_name = "Postgres" if "Postgres" in vacancy["description"] else "PostgreSQL"
                result = {"card":{"profession":"Аналитик","area":"Москва, Россия","work_format":self.format,
                                  "summary":"Анализ систем","structured_data":{"version":1}},
                          "tags":[{"category":"skill","name":tag_name,"requirement_level":"required","evidence":tag_name}],
                          "assessment":assessment}
                if vacancy["title"].startswith("Водитель"):
                    result["card"]["profession"] = "Водитель"
                    result["tags"] = [{"category":"license","name":"B","requirement_level":"required","evidence":"категория B"}]
                if vacancy["external_id"] == self.malformed_id:
                    result["tags"][0]["evidence"] = "Выдуманный фрагмент"
        return {"role":"assistant","content":json.dumps(result,ensure_ascii=False)}

    def count(self, mode):
        return sum(json.loads(messages[1]["content"])["input"].get("mode") == mode for messages, kwargs in self.calls)


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name)/"search.db")
        self.db.initialize()
        self.manager = ProfileManager.start_session(self.db)
        self.manager.create_profile("Аналитик",{"competencies":["SQL"]},authorized=True)
        self.settings = load_settings(ROOT/"config/v0.1.json")
        self.client = FixtureLLM()
        self.source = FixtureSource([raw()])

    def tearDown(self):
        self.temp.cleanup()

    def service(self, **limits):
        values = copy.deepcopy(self.settings.values)
        values["search"].update(limits)
        return SearchService(self.db,replace(self.settings,values=values),self.client,{"hh":self.source})

    def run_search(self, *, arguments=None, service=None):
        return collect((service or self.service()).search(self.manager,arguments or {},self.manager.get_state()))

    def test_complete_pipeline_saves_raw_card_tags_assessment_and_events(self):
        iterator = self.service().search(self.manager,{},self.manager.get_state())
        first = next(iterator)
        self.assertEqual(first["event"],"search_started")
        self.assertEqual(self.client.calls,[])
        report = collect(iterator)
        self.assertEqual(report["status"],"completed")
        self.assertFalse(report["is_partial"])
        self.assertEqual(report["counts"],{"found_count":1,"evaluated_count":1})
        with self.db.connection() as db:
            vacancy = db.execute("SELECT * FROM vacancies").fetchone()
            self.assertEqual(vacancy["description"],raw().description)
            self.assertEqual(db.execute("SELECT count(*) FROM vacancy_cards").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT canonical_name FROM tags").fetchone()[0],"PostgreSQL")
            self.assertEqual(db.execute("SELECT requirement_level FROM vacancy_tags").fetchone()[0],"required")
            self.assertIn("SQL",db.execute("SELECT reasoning FROM context_vacancies").fetchone()[0])
        self.assertIn("https://hh.ru/vacancy/100",report["summary"])

    def test_repeated_search_reuses_card_and_assessment_but_queries_source(self):
        self.run_search(); self.run_search()
        self.assertEqual(self.client.count("extract_and_assess"),1)
        self.assertEqual(self.client.count("assessment_only"),0)
        self.assertEqual(len(self.source.requests),2)
        with self.db.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM vacancies").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM task_vacancies").fetchone()[0],2)

    def test_profile_and_criteria_changes_only_reassess_existing_card(self):
        self.run_search()
        self.manager.update_profile({"notes":"Новый опыт"},authorized=True)
        self.run_search()
        self.manager.update_context({"salary":{"min":200000}},authorized=True)
        self.run_search()
        self.assertEqual(self.client.count("extract_and_assess"),1)
        self.assertEqual(self.client.count("assessment_only"),2)
        with self.db.connection() as db:
            row = db.execute("SELECT * FROM context_vacancies").fetchone()
            self.assertEqual((row["profile_revision"],row["criteria_revision"]),(2,2))

    def test_new_context_reuses_shared_card(self):
        self.run_search()
        self.manager.create_context("Другой поиск",authorized=True)
        report = self.run_search()
        self.assertEqual(self.client.count("extract_and_assess"),1)
        self.assertEqual(self.client.count("assessment_only"),1)
        self.assertTrue(report["vacancies"][0]["assessment_is_current"])

    def test_changed_source_text_invalidates_and_reextracts(self):
        self.run_search()
        self.source.items = [raw(description="Нужен SQL и PostgreSQL. Новые обязанности.")]
        self.run_search()
        self.assertEqual(self.client.count("extract_and_assess"),2)
        with self.db.connection() as db:
            row = db.execute("SELECT v.content_hash,c.source_hash FROM vacancies v JOIN vacancy_cards c ON c.vacancy_id=v.id").fetchone()
            self.assertEqual(row["content_hash"],row["source_hash"])

    def test_alias_resolution_saves_and_reuses_alias(self):
        self.run_search()
        self.client.target = 2
        self.source.items = [raw("101","Нужен SQL и Postgres. Работа удалённо.")]
        self.run_search()
        with self.db.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tags").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT normalized_alias FROM tag_aliases").fetchone()[0],"postgres")
        self.source.items = [raw("102","SQL и Postgres. Другая компания.")]
        self.client.target = 3
        before = len([m for m,k in self.client.calls if "сопоставления" in m[0]["content"]])
        self.run_search()
        after = len([m for m,k in self.client.calls if "сопоставления" in m[0]["content"]])
        self.assertEqual(before,after)

    def test_ambiguous_tag_does_not_merge_or_destroy_card(self):
        self.run_search()
        self.client.target = 2; self.client.ambiguous = True
        self.source.items = [raw("101","SQL и Postgres. Работа удалённо.")]
        self.run_search()
        with self.db.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tags").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM tag_aliases").fetchone()[0],0)
            card = db.execute("SELECT structured_data FROM vacancy_cards WHERE vacancy_id=2").fetchone()[0]
            self.assertIn("Неоднозначное",card)

    def test_captcha_after_first_result_is_partial_and_closes_source(self):
        self.client.target = 3
        self.source.items = [raw(),SourceError("CAPTCHA","Проверка пользователя")]
        report = self.run_search()
        self.assertEqual(report["status"],"completed")
        self.assertTrue(report["is_partial"])
        self.assertIn("CAPTCHA",report["stop_reason"])
        self.assertEqual(report["counts"]["evaluated_count"],1)
        self.assertEqual(self.source.closed,1)

    def test_source_error_without_results_is_failed(self):
        self.source.items = [SourceError("SOURCE_UNAVAILABLE","Нет доступа")]
        report = self.run_search()
        self.assertEqual(report["status"],"failed")
        self.assertFalse(report["is_partial"])

    def test_no_results_is_successful_empty_response(self):
        self.source.items = []
        report = self.run_search()
        self.assertEqual(report["status"],"completed")
        self.assertEqual(report["vacancies"],[])

    def test_invalid_tag_does_not_discard_valid_card_and_assessment(self):
        self.client.target = 2; self.client.malformed_id = "101"
        self.source.items = [raw(),raw("101")]
        report = self.run_search()
        self.assertFalse(report["is_partial"])
        self.assertEqual(report["counts"],{"found_count":2,"evaluated_count":2})
        self.assertEqual(report["diagnostics"]["metrics"]["tag_warnings"],1)
        with self.db.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM vacancy_cards").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT count(*) FROM vacancy_tags").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM vacancies").fetchone()[0],2)

    def test_analysis_limit_keeps_partial_results(self):
        self.client.target = 2; self.source.items = [raw(),raw("101")]
        report = self.run_search(service=self.service(max_new_analyses=1))
        self.assertIn("ANALYSIS_LIMIT",report["stop_reason"])
        self.assertEqual(report["counts"]["evaluated_count"],1)

    def test_core_failure_is_separate_from_results_and_not_retried_on_duplicate(self):
        self.client.target = 3
        self.run_search()
        with self.db.transaction() as db:
            db.execute("DELETE FROM vacancy_tags")
            db.execute("DELETE FROM vacancy_cards")
        original = self.client.complete
        def corrupt(messages,**kwargs):
            response = original(messages,**kwargs)
            if json.loads(messages[1]["content"])["input"].get("mode") == "extract_and_assess":
                result = json.loads(response["content"])
                result["assessment"]["reasoning"] = 17
                response["content"] = json.dumps(result)
            return response
        with patch.object(self.client,"complete",side_effect=corrupt):
            report = self.run_search()
        self.assertEqual(report["vacancies"],[])
        self.assertEqual(report["unassessed_count"],1)
        item = report["unassessed"][0]
        self.assertEqual((item["error_code"],item["path"]),("LLM_SCHEMA_INVALID","/assessment/reasoning"))
        self.assertNotIn("Не оценена",report["summary"])
        metrics = report["diagnostics"]["metrics"]
        self.assertEqual((metrics["analysis_attempts"],metrics["analysis_failed"],metrics["repair_requests"],metrics["duplicate_skipped"]),(1,1,1,1))
        with self.db.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM vacancy_cards").fetchone()[0],0)
        loaded = self.service().diagnostics(self.manager,self.manager.get_state())
        self.assertEqual(loaded["unassessed"][0]["path"],"/assessment/reasoning")
        diagnostic_file = self.db.path.parent / "search-diagnostics" / f"task-{report['task_id']}.json"
        self.assertNotIn(raw().description,diagnostic_file.read_text())

    def test_one_structural_repair_recovers_core_within_total_call_budget(self):
        original = self.client.complete
        broken = False
        def corrupt_once(messages,**kwargs):
            nonlocal broken
            response = original(messages,**kwargs)
            if json.loads(messages[1]["content"])["input"].get("mode") == "extract_and_assess" and not broken:
                broken = True
                response["content"] = '{"card":'
            return response
        with patch.object(self.client,"complete",side_effect=corrupt_once):
            report = self.run_search()
        self.assertEqual(report["counts"]["evaluated_count"],1)
        self.assertEqual(report["diagnostics"]["metrics"]["repair_requests"],1)
        self.assertEqual(self.client.count("extract_and_assess"),2)

    def test_repair_cannot_exceed_task_llm_budget(self):
        original = self.client.complete
        def corrupt(messages,**kwargs):
            response = original(messages,**kwargs)
            if json.loads(messages[1]["content"])["input"].get("mode") == "extract_and_assess":
                response["content"] = "bad JSON"
            return response
        with patch.object(self.client,"complete",side_effect=corrupt):
            report = self.run_search(service=self.service(max_llm_calls=2))
        self.assertIn("LLM_LIMIT",report["stop_reason"])
        self.assertEqual(len(self.client.calls),2)
        self.assertEqual(report["diagnostics"]["metrics"]["analysis_failed"],1)

    def test_whitespace_only_evidence_difference_preserves_actual_source_quote(self):
        original = self.client.complete
        self.source.items = [raw(description="Нужен SQL. PostgreSQL\n  REST API")]
        def spaced(messages,**kwargs):
            response = original(messages,**kwargs)
            result = json.loads(response["content"])
            if "tags" in result:
                result["tags"][0]["evidence"] = "PostgreSQL REST API"
                response["content"] = json.dumps(result)
            return response
        with patch.object(self.client,"complete",side_effect=spaced):
            report = self.run_search()
        self.assertEqual(report["diagnostics"]["metrics"]["tag_warnings"],0)
        with self.db.connection() as db:
            self.assertEqual(db.execute("SELECT evidence FROM vacancy_tags").fetchone()[0],"PostgreSQL\n  REST API")

    def test_invalid_resolver_id_does_not_destroy_second_card(self):
        self.run_search(); self.client.target = 2
        self.source.items = [raw("101","SQL и Postgres")]
        original = self.client.complete
        def wrong_id(messages,**kwargs):
            response = original(messages,**kwargs)
            if "сопоставления" in messages[0]["content"]:
                response["content"] = json.dumps({"decision":"use_existing","tag_id":999999})
            return response
        with patch.object(self.client,"complete",side_effect=wrong_id):
            report = self.run_search()
        self.assertEqual(report["counts"]["evaluated_count"],2)
        self.assertEqual(report["diagnostics"]["metrics"]["tag_warnings"],1)
        with self.db.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM vacancy_cards").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT count(*) FROM tag_aliases").fetchone()[0],0)

    def test_exhausted_tag_resolution_budget_preserves_completed_core(self):
        self.run_search(); self.client.target = 2
        self.source.items = [raw("101","SQL и Postgres")]
        report = self.run_search(service=self.service(max_llm_calls=2))
        self.assertEqual(report["counts"]["evaluated_count"],2)
        self.assertEqual(report["diagnostics"]["metrics"]["tag_warnings"],1)
        self.assertEqual(report["diagnostics"]["metrics"]["analysis_succeeded"],1)

    def test_read_reassessment_failure_keeps_processing_other_cards(self):
        self.client.target = 2; self.source.items = [raw(),raw("101")]
        report = self.run_search()
        self.manager.update_context({"notes":"Изменились условия"},authorized=True)
        original = self.client.complete
        def failing(messages,**kwargs):
            response = original(messages,**kwargs)
            data = json.loads(messages[1]["content"])["input"]
            if data.get("mode") == "assessment_only" and data["card"]["vacancy_id"] == 1:
                response["content"] = "broken"
            return response
        with patch.object(self.client,"complete",side_effect=failing):
            result = collect(self.service().get_results(self.manager,{},self.manager.get_state()))
        self.assertEqual(result["counts"]["evaluated_count"],1)
        self.assertEqual(result["unassessed_count"],1)
        self.assertEqual(result["unassessed"][0]["error_code"],"LLM_JSON_INVALID")
        self.assertEqual(result["diagnostics"]["metrics"]["reassessments"],2)

    def test_unreadable_source_page_does_not_abort_following_vacancy(self):
        self.client.target = 3
        self.source.items = [SourceEvent("source_warning","VACANCY_UNREADABLE","Нет полного текста.","99","https://hh.ru/vacancy/99","source_extraction"),raw()]
        iterator = self.service().search(self.manager,{},self.manager.get_state())
        events = [next(iterator),next(iterator)]
        report = collect(iterator)
        self.assertEqual(report["counts"]["evaluated_count"],1)
        self.assertEqual(report["diagnostics"]["metrics"]["source_skipped"],1)
        self.assertIn("source_warning",[event["event"] for event in events])
        self.assertTrue(report["is_partial"])

    def test_legacy_remote_location_is_recovered_without_mutating_criteria(self):
        self.manager.update_context({"locations":["удалённо"]},authorized=True)
        state = self.manager.get_state()
        self.run_search()
        self.assertEqual(self.source.requests[0]["areas"],[])
        self.assertEqual(self.source.requests[0]["filters"]["work_formats"],["remote"])
        self.assertEqual(self.manager.get_state(),state)

    def test_source_limit_and_total_llm_limit(self):
        self.client.target = 3; self.source.items = [raw(),raw("101")]
        report = self.run_search(service=self.service(max_source_vacancies=1))
        self.assertIn("SOURCE_LIMIT",report["stop_reason"])
        with self.db.transaction() as db:
            db.execute("DELETE FROM context_vacancies")
        report = self.run_search(service=self.service(max_llm_calls=1))
        self.assertIn("LLM_LIMIT",report["stop_reason"])

    def test_explicit_geography_and_format_override_planner_omission(self):
        self.manager.update_context({"locations":["Москва"],"work_formats":["remote"]},authorized=True)
        self.run_search()
        query = self.source.requests[0]
        self.assertEqual(query["areas"],["Москва"])
        self.assertEqual(query["filters"]["work_formats"],["remote"])

    def test_proven_hard_format_conflict_is_not_presented(self):
        self.manager.update_context({"work_formats":["remote"]},authorized=True)
        self.client.format = "office"
        report = self.run_search()
        self.assertEqual(report["vacancies"],[])
        self.assertEqual(report["counts"]["evaluated_count"],1)

    def test_one_off_request_preserves_saved_criteria(self):
        before = self.manager.get_state()
        self.run_search(arguments={"request":"Изучи одну вакансию в EdTech"})
        self.assertEqual(self.manager.get_state(),before)
        with self.db.connection() as db:
            plan = json.loads(db.execute("SELECT search_queries FROM search_tasks").fetchone()[0])
            self.assertEqual(set(plan),{"version","queries"})

    def test_results_and_details_are_context_scoped(self):
        service = self.service(); report = self.run_search(service=service)
        details = collect(service.get_details(self.manager,{"vacancy_id":report["vacancies"][0]["vacancy_id"]},self.manager.get_state()))
        self.assertEqual(details["description"],raw().description)
        self.assertTrue(details["source_text_untrusted"])
        self.manager.create_context("Другой",authorized=True)
        with self.assertRaises(ProfileError):
            collect(service.get_results(self.manager,{"task_id":report["task_id"]},self.manager.get_state()))
        with self.assertRaises(ProfileError):
            collect(service.get_details(self.manager,{"vacancy_id":details["vacancy_id"]},self.manager.get_state()))

    def test_reading_results_refreshes_scores_without_source_or_extraction(self):
        service = self.service(); report = self.run_search(service=service)
        self.manager.update_context({"notes":"Новые условия"},authorized=True)
        refreshed = collect(service.get_results(self.manager,{"task_id":report["task_id"]},self.manager.get_state()))
        self.assertTrue(refreshed["vacancies"][0]["assessment_is_current"])
        self.assertEqual(self.client.count("extract_and_assess"),1)
        self.assertEqual(self.client.count("assessment_only"),1)
        self.assertEqual(len(self.source.requests),1)

    def test_stream_close_marks_saved_result_partial(self):
        self.client.target = 3
        iterator = self.service().search(self.manager,{},self.manager.get_state())
        self.assertEqual(next(iterator)["event"],"search_started")
        self.assertEqual(next(iterator)["event"],"search_progress")
        iterator.close()
        with self.db.connection() as db:
            row = db.execute("SELECT * FROM search_tasks").fetchone()
            self.assertEqual(row["state"],"completed")
            self.assertEqual(row["is_partial"],1)
            self.assertIn("INTERRUPTED",row["stop_reason"])
        self.assertEqual(self.source.closed,1)

    def test_unknown_planner_source_fails_before_collection(self):
        self.client.plan_source = "not-connected"
        report = self.run_search()
        self.assertEqual(report["status"],"failed")
        self.assertEqual(self.source.requests,[])

    def test_recovery_does_not_leave_running_tasks_after_restart(self):
        self.run_search()
        with self.db.transaction() as db:
            db.execute("UPDATE search_tasks SET state='running',completed_at=NULL")
        tasks.recover_interrupted(self.db)
        with self.db.connection() as db:
            row = db.execute("SELECT state,is_partial,stop_reason FROM search_tasks").fetchone()
            self.assertEqual((row["state"],row["is_partial"]),("completed",1))
            self.assertIn("INTERRUPTED",row["stop_reason"])

    def test_expired_task_stops_before_any_llm_or_source_call(self):
        import time
        def expired(settings):
            budget = TaskBudget(settings)
            budget.deadline = time.monotonic()-1
            return budget
        service = SearchService(self.db,self.settings,self.client,{"hh":self.source},budget_factory=expired)
        report = self.run_search(service=service)
        self.assertEqual(report["status"],"failed")
        self.assertIn("TIMEOUT",report["stop_reason"])
        self.assertEqual(self.client.calls,[])
        self.assertEqual(self.source.requests,[])

    def test_bundle_failure_rolls_back_new_card_tags_and_assessment(self):
        import sqlite3
        from unittest.mock import patch
        self.run_search()
        self.client.target = 2
        self.source.items = [raw("101")]
        with patch("jobradar.storage.vacancies.save_assessment",side_effect=sqlite3.IntegrityError("failure")):
            report = self.run_search()
        self.assertTrue(report["is_partial"])
        with self.db.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM vacancies").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT count(*) FROM vacancy_cards").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM vacancy_tags").fetchone()[0],1)

    def test_shared_collection_cap_includes_cached_candidates(self):
        self.run_search()
        self.client.target = 5
        self.source.items = [raw("101"),raw("102")]
        report = self.run_search(service=self.service(max_source_vacancies=2))
        self.assertEqual(report["counts"]["found_count"],2)
        self.assertIn("SOURCE_LIMIT",report["stop_reason"])

    def test_source_change_invalidates_assessments_in_other_contexts(self):
        self.run_search()
        first_context = self.manager.get_state()["active_context_id"]
        self.manager.create_context("Другой",authorized=True)
        self.run_search()
        self.source.items = [raw(description="SQL и PostgreSQL. Объявление изменено.")]
        self.run_search()
        with self.db.connection() as db:
            old = db.execute("SELECT profile_revision,reasoning FROM context_vacancies WHERE context_id=?",(first_context,)).fetchone()
            self.assertIsNone(old["profile_revision"])
            self.assertTrue(old["reasoning"])


if __name__ == "__main__":
    unittest.main()
