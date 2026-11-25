"""Saved context geography versus model proposals; disposable SQLite only."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tests/v01'))
from test_search import FixtureLLM, FixtureSource, raw
from jobradar.config import load_settings
from jobradar.events import collect
from jobradar.profiles import ProfileManager
from jobradar.search.service import SearchService
from jobradar.storage import Database
from jobradar.tools.registry import DEFINITIONS, ToolRegistry

REPEAT='давай снова поищем вакансии системного аналитика'


class GeographyRegressions(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.db=Database(Path(self.temp.name)/'test.sqlite3');self.db.initialize()
        self.manager=ProfileManager.start_session(self.db)
        self.manager.create_profile('Системный аналитик',authorized=True)
        self.manager.update_context({'locations':['Россия'],'work_formats':['remote']},authorized=True)
        self.settings=load_settings(ROOT/'config/v0.2.json')
        self.client=FixtureLLM();self.source=FixtureSource([raw('100')])
        self.areas=['Россия','Казахстан'];self.change=None;self.resolutions=None
        original=self.client.complete
        def complete(messages,**kwargs):
            result=original(messages,**kwargs)
            if 'планирования' in messages[0]['content']:
                output=json.loads(result['content']);output['search_queries']['queries'][0]['areas']=list(self.areas)
                if self.change is not None:output['geography_change']=copy.deepcopy(self.change)
                if self.resolutions is not None:output['resolved_locations']=copy.deepcopy(self.resolutions)
                result['content']=json.dumps(output)
            return result
        self.client.complete=complete

    def tearDown(self):self.temp.cleanup()

    def service(self):return SearchService(self.db,self.settings,self.client,{'hh':self.source})

    def run_search(self,request=REPEAT):
        return collect(self.service().search(self.manager,{'request':request},self.manager.get_state()))

    def test_repeat_ignores_model_added_regions_and_keeps_saved_hard_conditions(self):
        before=self.manager.get_state();report=self.run_search()
        self.assertEqual(len(self.source.requests),1)
        self.assertEqual(self.source.requests[0]['areas'],['Россия'])
        self.assertNotIn('LOCATION_CLARIFICATION',report['stop_reason'] or '')
        self.assertEqual(report['hh_counts']['hh_saved'],1)
        self.assertEqual(self.manager.get_state(),before)

    def test_repeat_ignores_completely_different_and_missing_model_regions(self):
        for areas in [['Казахстан'],[],['113']]:
            with self.subTest(areas=areas):
                self.areas=areas;self.source.requests.clear();self.run_search()
                self.assertEqual(self.source.requests[0]['areas'],['Россия'])

    def test_active_other_context_is_used_and_never_switched_by_profession_words(self):
        first=self.manager.get_state()['active_context_id']
        second=self.manager.create_context('Другой поиск',{'locations':['Москва']},authorized=True)['context_id']
        self.areas=['Россия'];before=self.manager.get_state();report=self.run_search()
        self.assertEqual(self.source.requests[0]['areas'],['Москва'])
        self.assertEqual(self.manager.get_state(),before)
        with self.db.connection() as db:
            self.assertEqual(db.execute('select context_id from search_tasks where id=?',(report['task_id'],)).fetchone()[0],second)
        self.assertNotEqual(first,second)

    def test_no_saved_geography_does_not_accept_unsolicited_model_restrictions(self):
        self.manager.update_context({'locations':[]},authorized=True)
        self.areas=['Москва'];self.run_search()
        self.assertEqual(self.source.requests[0]['areas'],[])

    def test_explicit_different_geography_asks_scope_without_saving_or_source_access(self):
        request='Разово найди вакансии в Казахстане';self.areas=['Казахстан']
        self.change={'locations':['Казахстан'],'evidence':'в Казахстане'}
        before=self.manager.get_state();report=self.run_search(request)
        self.assertEqual(self.source.requests,[])
        self.assertIn('LOCATION_CLARIFICATION',report['stop_reason'])
        self.assertIn('географ',report['summary'])
        self.assertEqual(self.manager.get_state(),before)

    def test_explicit_removal_of_region_limit_does_not_widen_search_without_scope(self):
        self.change={'locations':[],'evidence':'без ограничения по регионам'}
        before=self.manager.get_state();report=self.run_search('Ищи без ограничения по регионам')
        self.assertEqual(self.source.requests,[])
        self.assertIn('LOCATION_CLARIFICATION',report['stop_reason'])
        self.assertEqual(self.manager.get_state(),before)

    def test_explicit_same_geography_is_not_a_change(self):
        self.change={'locations':['россия'],'evidence':'Россия'}
        self.run_search('Найди вакансии, география Россия')
        self.assertEqual(self.source.requests[0]['areas'],['Россия'])

    def test_geography_change_requires_evidence_from_current_turn(self):
        self.change={'locations':['Казахстан'],'evidence':'в Казахстане'}
        before=self.manager.get_state();report=self.run_search()
        self.assertEqual(self.source.requests,[])
        self.assertIn('LOCATION_CLARIFICATION',report['stop_reason'])
        self.assertEqual(self.manager.get_state(),before)

    def test_unresolved_saved_group_still_requires_clarification(self):
        self.manager.update_context({'locations':['ближнее зарубежье']},authorized=True)
        report=self.run_search();self.assertEqual(self.source.requests,[])
        self.assertIn('LOCATION_CLARIFICATION',report['stop_reason'])

    def test_resolved_saved_group_is_not_expanded_by_proposed_query_areas(self):
        self.manager.update_context({'locations':['ближнее зарубежье']},authorized=True)
        self.resolutions=[{'input':'ближнее зарубежье','areas':['Беларусь'],'explanation':'Страна, ранее выбранная пользователем.'}]
        self.areas=['Беларусь','Казахстан'];self.run_search()
        self.assertEqual(self.source.requests[0]['areas'],['Беларусь'])

    def test_diagnostics_keep_proposed_regions_and_start_context_after_criteria_change(self):
        before=self.manager.get_state();report=self.run_search()
        self.manager.update_context({'locations':['Москва']},authorized=True)
        self.client.calls.clear()
        diagnostic=self.service().diagnostics(self.manager,self.manager.get_state(),report['task_id'])
        self.assertEqual(diagnostic['active_context']['saved_locations'],['Москва'])
        start=diagnostic['diagnostics']['context_at_start']
        self.assertEqual(start['saved_locations'],['Россия'])
        self.assertEqual(start['context_id'],before['active_context_id'])
        event=next(i for i in diagnostic['diagnostics']['issues'] if i['code']=='LOCATION_PLAN')
        self.assertEqual(event['details']['proposed_queries'][0]['areas'],['Россия','Казахстан'])
        self.assertEqual(diagnostic['search_plan']['queries'][0]['areas'],['Россия'])
        self.assertEqual(self.client.calls,[])

    def test_failed_explicit_change_keeps_geography_evidence_in_errors_capture_mode(self):
        self.change={'locations':['Казахстан'],'evidence':'в Казахстане'}
        report=self.run_search('Найди в Казахстане')
        diagnostic=self.service().diagnostics(self.manager,self.manager.get_state(),report['task_id'])
        self.assertIsNone(diagnostic['search_plan'])
        self.assertTrue(any(i['code']=='LOCATION_PLAN' for i in diagnostic['diagnostics']['issues']))
        self.assertEqual(diagnostic['diagnostics']['context_at_start']['saved_locations'],['Россия'])
        self.assertFalse(any('response' in call for call in diagnostic['diagnostics']['llm_calls']))

    def test_delete_tools_are_absent_and_cannot_be_registered(self):
        self.assertEqual(len(DEFINITIONS),11)
        self.assertFalse(any('delete' in name or 'remove' in name for name in DEFINITIONS))
        for name in ['delete_profile','delete_context']:
            with self.assertRaises(ValueError):ToolRegistry().register(name,lambda *args:None)


if __name__=='__main__':unittest.main()
