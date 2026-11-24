"""Algorithm 1.0 acceptance against real SQLite, deterministic LLM and sources."""

import copy
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"tests/v01"))
from test_search import FixtureLLM, FixtureSource, raw

from jobradar.config import load_settings
from jobradar.events import collect
from jobradar.llm.client import LLMError
from jobradar.profiles import ProfileManager
from jobradar.search.service import SearchService
from jobradar.sources.base import RawVacancy, SourceError, SourceEvent
from jobradar.storage import Database, vacancies


class AlgorithmTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name)/"search.sqlite3");self.db.initialize()
        self.manager = ProfileManager.start_session(self.db)
        self.manager.create_profile("Аналитик",{"competencies":["SQL"]},authorized=True)
        self.settings = load_settings(ROOT/"config/v0.1.json")
        self.client, self.source = FixtureLLM(), FixtureSource([])

    def tearDown(self):
        self.temp.cleanup()

    def service(self, **limits):
        values=copy.deepcopy(self.settings.values);values["search"].update(limits)
        return SearchService(self.db,replace(self.settings,values=values),self.client,{"hh":self.source})

    def run_search(self, **limits):
        return collect(self.service(**limits).search(self.manager,{},self.manager.get_state()))

    def store(self, *items):
        with self.db.transaction() as db:
            return [vacancies.upsert_raw(db,item) for item in items]

    def calls(self, mode):
        return [json.loads(messages[1]["content"])["input"] for messages,kw in self.client.calls
                if json.loads(messages[1]["content"])["input"].get("mode")==mode]

    def test_current_cards_are_assessed_before_recent_raw_and_not_reextracted(self):
        self.store(raw("1"));self.run_search()
        self.store(raw("2"))
        self.manager.update_context({"notes":"Новые условия"},authorized=True)
        self.client.calls.clear();self.run_search()
        modes=[json.loads(m[1]["content"])["input"].get("mode") for m,kw in self.client.calls]
        self.assertLess(modes.index("assessment_only"),modes.index("extract_and_assess"))
        self.assertEqual([c["vacancy"]["external_id"] for c in self.calls("extract_and_assess")],["2"])

    def test_stale_card_is_raw_and_new_description_is_used(self):
        self.store(raw("1"));self.run_search()
        self.store(replace(raw("1"),title="Необычная роль",description="Новые задачи SQL и PostgreSQL"))
        self.client.calls.clear();self.run_search()
        self.assertEqual(len(self.calls("extract_and_assess")),1)
        self.assertIn("Новые задачи",self.calls("extract_and_assess")[0]["vacancy"]["description"])
        self.assertEqual(self.calls("assessment_only"),[])

    def test_current_card_mismatch_is_rejected_before_paid_reassessment(self):
        self.store(raw("1"));self.run_search()
        self.manager.update_context({"work_formats":["office"]},authorized=True)
        self.client.calls.clear();report=self.run_search()
        self.assertEqual(self.calls("assessment_only"),[])
        self.assertEqual(report["vacancies"],[])
        self.assertEqual(report["filtered_count"],1)

    def test_local_raw_proven_mismatch_is_saved_and_can_match_another_context(self):
        self.store(raw("1","Формат работы: офис\nТребуется SQL и PostgreSQL"))
        self.manager.update_context({"work_formats":["remote"]},authorized=True)
        report=self.run_search()
        self.assertEqual(self.calls("extract_and_assess"),[])
        self.assertEqual(report["filtered_count"],1)
        self.assertEqual(report["unassessed_count"],0)
        with self.db.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM vacancies").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM vacancy_cards").fetchone()[0],0)
        self.manager.create_context("Офис",criteria={"work_formats":["office"]},authorized=True)
        self.client.format="office";report=self.run_search()
        self.assertEqual(report["counts"]["evaluated_count"],1)

    def test_new_hh_raw_is_prefiltered_before_extraction(self):
        self.manager.update_context({"work_formats":["remote"]},authorized=True)
        self.source.items=[raw("1","Формат работы: офис\nSQL и PostgreSQL")]
        report=self.run_search()
        self.assertEqual(self.calls("extract_and_assess"),[])
        self.assertEqual(report["hh_counts"]["hh_saved"],1)
        self.assertEqual(report["filtered_count"],1)

    def test_adjacent_role_and_office_mentions_are_not_rejected_on_keyword_score(self):
        self.manager.update_context({"work_formats":["remote"]},authorized=True)
        candidate=replace(raw("1","Задачи связаны с PostgreSQL. Обучение в офисе клиента возможно."),title="Инженер интеграций")
        self.store(candidate);report=self.run_search()
        self.assertEqual(len(self.calls("extract_and_assess")),1)
        self.assertEqual(report["filtered_count"],0)

    def test_zero_keyword_score_is_low_priority_not_a_hard_rejection(self):
        self.store(replace(raw("1","Неоднозначное описание роли"),title="Другое название",employer="Организация"))
        report=self.run_search()
        self.assertEqual(len(self.calls("extract_and_assess")),1)
        self.assertEqual(report["filtered_count"],0)

    def test_ambiguous_alternative_format_does_not_become_a_proven_mismatch(self):
        self.manager.update_context({"work_formats":["remote"]},authorized=True)
        self.source.items=[raw("1","Формат работы: офис\nФормат работы: удалённо или гибрид\nSQL и PostgreSQL")]
        report=self.run_search()
        self.assertEqual(len(self.calls("extract_and_assess")),1)
        self.assertEqual(report["filtered_count"],0)

    def test_local_candidates_do_not_spend_hh_quota_and_target_does_not_stop_collection(self):
        self.store(raw("1"),raw("2"));self.run_search()
        self.source.items=[raw("3"),raw("4")];report=self.run_search(max_source_vacancies=2)
        self.assertEqual(report["counts"]["found_count"],4)
        self.assertEqual(report["hh_counts"]["hh_discovered"],2)
        self.assertEqual(report["counts"]["evaluated_count"],4)
        self.assertFalse(report["coverage"]["source_complete"])

    def test_duplicates_across_queries_are_counted_once_and_not_reanalyzed(self):
        original=self.client.complete
        def two_queries(messages,**kwargs):
            response=original(messages,**kwargs)
            if "планирования" in messages[0]["content"]:
                result=json.loads(response["content"]);q=result["search_queries"]["queries"][0]
                result["search_queries"]["queries"].append({**q,"text":"API интеграции"})
                response["content"]=json.dumps(result)
            return response
        self.client.complete=two_queries
        self.source.items=[raw("1"),raw("1"),raw("2")]
        report=self.run_search()
        self.assertEqual(len(self.source.requests),2)
        self.assertEqual(report["hh_counts"]["hh_discovered"],2)
        self.assertEqual(report["hh_counts"]["hh_read"],2)
        self.assertGreaterEqual(report["hh_counts"]["hh_duplicates"],2)
        self.assertEqual(len(self.calls("extract_and_assess")),2)

    def test_analysis_limit_defers_cards_but_all_hh_raws_are_saved(self):
        self.source.items=[raw(str(i)) for i in range(15)]
        report=self.run_search(max_new_analyses=1)
        self.assertEqual(report["hh_counts"]["hh_saved"],15)
        self.assertEqual(report["counts"]["evaluated_count"],1)
        self.assertEqual(report["diagnostics"]["metrics"]["deferred"],14)
        self.assertIn("ANALYSIS_LIMIT",report["stop_reason"])

    def test_total_llm_limit_does_not_stop_source(self):
        self.source.items=[raw(str(i)) for i in range(12)]
        report=self.run_search(max_llm_calls=1)
        self.assertEqual(report["hh_counts"]["hh_saved"],12)
        self.assertEqual(self.calls("extract_and_assess"),[])
        self.assertIn("LLM_LIMIT",report["stop_reason"])

    def test_all_source_portion_is_saved_before_first_failed_analysis(self):
        self.source.items=[raw(str(i)) for i in range(15)]
        original=self.client.complete;observed=[]
        def failing(messages,**kwargs):
            data=json.loads(messages[1]["content"])["input"]
            if data.get("mode")=="extract_and_assess":
                with self.db.connection() as db:
                    observed.append(db.execute("SELECT count(*) FROM vacancies").fetchone()[0])
                raise LLMError("Тестовый отказ")
            return original(messages,**kwargs)
        self.client.complete=failing;report=self.run_search()
        self.assertEqual(observed[0],self.service().BATCH_SIZE)
        self.assertEqual(report["hh_counts"]["hh_saved"],15)
        with self.db.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM vacancies").fetchone()[0],15)

    def test_results_are_streamed_before_collection_finishes_with_unique_snapshot_ids(self):
        self.source.items=[raw(str(i)) for i in range(15)]
        iterator=self.service().search(self.manager,{},self.manager.get_state())
        snapshots=[]
        while True:
            event=next(iterator)
            if event["event"]=="search_results":
                snapshots.append(event["data"])
                if len(snapshots)==1:
                    self.assertEqual(self.source.closed,0)
                    self.assertLess(len(event["data"]["vacancies"]),15)
            if event["event"]=="search_finished":
                break
        report=collect(iterator)
        self.assertGreater(len(snapshots),1)
        for snapshot in snapshots:
            ids=[item["vacancy_id"] for item in snapshot["vacancies"]]
            self.assertEqual(len(ids),len(set(ids)))
            self.assertTrue(snapshot["replace"])
        self.assertEqual(report["result_count"],15)

    def test_metadata_only_is_deferred_and_cannot_destroy_full_source(self):
        identifier=self.store(raw("1"))[0]
        with self.db.transaction() as db:
            before=vacancies.get_raw(db,identifier)
            vacancies.upsert_raw(db,replace(raw("1"),description=""))
            after=vacancies.get_raw(db,identifier)
        self.assertEqual((after["description"],after["content_hash"]),(before["description"],before["content_hash"]))
        self.source.items=[replace(raw("2"),description="")];report=self.run_search()
        self.assertEqual(report["hh_counts"]["hh_incomplete"],1)
        self.assertTrue(any(item["error_code"]=="SOURCE_INCOMPLETE" for item in report["unassessed"]))

    def test_source_error_preserves_and_processes_last_incomplete_portion(self):
        self.source.items=[raw("1"),SourceError("CAPTCHA","Тестовая проверка")]
        report=self.run_search()
        self.assertEqual(report["counts"]["evaluated_count"],1)
        self.assertIn("CAPTCHA",report["stop_reason"])
        self.assertFalse(report["coverage"]["source_complete"])

    def test_changed_source_invalidates_streamed_old_result_even_when_reanalysis_fails(self):
        self.store(raw("1"));self.run_search()
        self.source.items=[raw("1","Новые обязанности SQL и PostgreSQL")]
        original=self.client.complete
        def failing(messages,**kwargs):
            data=json.loads(messages[1]["content"])["input"]
            if data.get("mode")=="extract_and_assess":
                raise LLMError("Тестовый отказ")
            return original(messages,**kwargs)
        self.client.complete=failing
        iterator=self.service().search(self.manager,{},self.manager.get_state())
        snapshots=[]
        while True:
            event=next(iterator)
            if event["event"]=="search_results":snapshots.append(event["data"]["vacancies"])
            if event["event"]=="search_finished":break
        report=collect(iterator)
        self.assertTrue(snapshots[0]);self.assertEqual(snapshots[-1],[])
        self.assertEqual(report["counts"]["evaluated_count"],0)

    def test_source_deadline_exit_cannot_be_reported_as_complete_coverage(self):
        from jobradar.search.budget import TaskBudget
        clock=[0]
        def budget_factory(settings):return TaskBudget(settings,clock=lambda:clock[0])
        class ExpiringSource:
            def search(inner,*args,**kwargs):
                clock[0]=1000
                yield from ()
        service=SearchService(self.db,self.settings,self.client,{"hh":ExpiringSource()},budget_factory=budget_factory)
        report=collect(service.search(self.manager,{},self.manager.get_state()))
        self.assertIn("TIMEOUT",report["stop_reason"])
        self.assertFalse(report["coverage"]["source_complete"])

    def test_confirmed_closure_removes_previously_ready_result_without_deleting_card(self):
        self.store(raw("1"));self.run_search()
        self.source.items=[SourceEvent("source_warning","VACANCY_CLOSED","Закрыто","1")]
        report=self.run_search()
        self.assertEqual(report["vacancies"],[])
        detail=collect(self.service().get_details(self.manager,{"vacancy_id":1},self.manager.get_state()))
        self.assertEqual(detail["status"],"closed")
        with self.db.connection() as db:
            self.assertEqual(db.execute("SELECT status FROM vacancies").fetchone()[0],"closed")
            self.assertEqual(db.execute("SELECT count(*) FROM vacancy_cards").fetchone()[0],1)

    def test_observed_reopening_can_reuse_card_after_local_closed_filter(self):
        self.store(raw("1"));self.run_search()
        with self.db.transaction() as db:vacancies.mark_closed(db,"hh","1")
        self.client.calls.clear();self.source.items=[raw("1")]
        report=self.run_search()
        self.assertEqual(len(report["vacancies"]),1)
        self.assertEqual(report["filtered_count"],0)
        self.assertEqual(self.calls("extract_and_assess"),[])

    def test_already_received_raw_is_saved_at_deadline_without_starting_analysis(self):
        from jobradar.search.budget import TaskBudget
        clock=[0]
        def budget_factory(settings):return TaskBudget(settings,clock=lambda:clock[0])
        class LastSource:
            def search(inner,*args,**kwargs):
                clock[0]=1000
                yield raw("1")
        service=SearchService(self.db,self.settings,self.client,{"hh":LastSource()},budget_factory=budget_factory)
        report=collect(service.search(self.manager,{},self.manager.get_state()))
        self.assertEqual(report["hh_counts"]["hh_saved"],1)
        self.assertEqual(self.calls("extract_and_assess"),[])
        self.assertIn("TIMEOUT",report["stop_reason"])
