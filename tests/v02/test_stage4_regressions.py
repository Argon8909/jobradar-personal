"""Cross-component acceptance regressions; all transport and source data are local."""
import copy
import io
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock
from urllib.error import URLError

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tests/v01'))
from test_search import FixtureLLM, FixtureSource, raw
from test_dialogue import FakeClient, intent, calls, answer
from jobradar.analysis.modules import StructuredLLM
from jobradar.config import load_settings
from jobradar.dialogue.agent import DialogueAgent
from jobradar.events import collect
from jobradar.llm.client import DeepSeekClient, LLMError
from jobradar.profiles import ProfileManager
from jobradar.search.budget import TaskBudget, SearchStop
from jobradar.search.service import SearchService
from jobradar.search.trace import TaskTrace
from jobradar.storage import Database, vacancies


def response(value):
    return io.BytesIO(json.dumps({'choices':[{'message':{'role':'assistant','content':json.dumps(value)},'finish_reason':'stop'}],
                                 'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15}}).encode())


class Stage4Regressions(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.db=Database(self.root/'test.sqlite3');self.db.initialize()
        self.manager=ProfileManager.start_session(self.db);self.manager.create_profile('Аналитик',authorized=True)
        self.settings=load_settings(ROOT/'config/v0.2.json')
        self.client,self.source=FixtureLLM(),FixtureSource([])

    def tearDown(self):self.temp.cleanup()

    def service(self,**limits):
        values=copy.deepcopy(self.settings.values);values['search'].update(limits)
        return SearchService(self.db,replace(self.settings,values=values),self.client,{'hh':self.source})

    def store(self,*items):
        with self.db.transaction() as db:
            return [vacancies.upsert_raw(db,item) for item in items]

    def search(self,request=None,**limits):
        return collect(self.service(**limits).search(self.manager,{'request':request} if request else {},self.manager.get_state()))

    def test_hh_discovery_quota_does_not_cap_local_result_reassessment(self):
        self.store(raw('1'),raw('2'));report=self.search()
        self.manager.update_context({'notes':'Уточнённые условия'},authorized=True)
        self.client.calls.clear()
        result=collect(self.service(max_source_vacancies=1).get_results(self.manager,{'task_id':report['task_id']},self.manager.get_state()))
        self.assertEqual(result['result_count'],2)
        self.assertEqual(self.client.count('assessment_only'),2)
        self.assertEqual(self.client.count('extract_and_assess'),0)
        self.assertEqual(self.source.requests and len(self.source.requests),1)  # only the original search

    def test_detail_refresh_honors_persisted_task_period_after_trace_pruning(self):
        old=(datetime.now(timezone.utc)-timedelta(days=15)).isoformat()
        self.store(replace(raw('1'),published_at=old));self.search()
        original=self.client.complete
        def weekly(messages,**kwargs):
            result=original(messages,**kwargs)
            if 'планирования' in messages[0]['content']:
                value=json.loads(result['content']);value['publication_period']={'unit':'weeks','value':1,'evidence':'не старше недели'}
                result['content']=json.dumps(value)
            return result
        self.client.complete=weekly;task=self.search('Ищи не старше недели')
        self.assertEqual(task['vacancies'],[])
        TaskTrace(self.db,task['task_id']).path.unlink()
        self.manager.update_context({'notes':'Уточнение'},authorized=True);self.client.calls.clear()
        result=collect(self.service().get_details(self.manager,{'vacancy_id':1},self.manager.get_state()))
        self.assertEqual(result['vacancy_id'],1)
        self.assertEqual(self.client.count('assessment_only'),0)

    def test_transport_retry_must_obey_shared_specialized_budget(self):
        values=copy.deepcopy(self.settings.values);values['search']['max_llm_calls']=1
        settings=replace(self.settings,values=values);budget=TaskBudget(settings)
        opener=Mock(side_effect=[URLError('offline'),response({'ok':True})])
        client=DeepSeekClient(settings,api_key='test-only',opener=opener,sleeper=lambda _:None)
        llm=StructuredLLM(client,settings,budget)
        with self.assertRaises(SearchStop) as error:
            llm.call('tag_resolver',{}, {'type':'object'})
        self.assertEqual(error.exception.code,'LLM_LIMIT')
        self.assertEqual(opener.call_count,1)
        self.assertEqual(budget.calls,1)

    def test_allowed_transport_retry_is_charged_and_raw_collection_survives_exhaustion(self):
        values=copy.deepcopy(self.settings.values);values['search']['max_llm_calls']=2
        settings=replace(self.settings,values=values)
        plan={'search_queries':{'version':1,'queries':[{'source':'hh','text':'Аналитик SQL'}]},'target_count':1}
        attempts=[]
        def transport(*args,**kwargs):
            attempts.append(1)
            if len(attempts)==1:return response(plan)
            raise URLError('offline')
        opener=Mock(side_effect=transport)
        client=DeepSeekClient(settings,api_key='test-only',opener=opener,sleeper=lambda _:None)
        self.source.items=[raw(str(i)) for i in range(12)]
        service=SearchService(self.db,settings,client,{'hh':self.source})
        result=collect(service.search(self.manager,{},self.manager.get_state()))
        self.assertEqual(opener.call_count,2)
        self.assertEqual(result['hh_counts']['hh_saved'],12)
        self.assertIn('LLM_LIMIT',result['stop_reason'])
        self.assertEqual(result['diagnostics']['metrics']['specialized_llm_calls'],2)

    def test_retry_success_counts_two_attempts_without_changing_response_contract(self):
        values=copy.deepcopy(self.settings.values);values['search']['max_llm_calls']=2
        settings=replace(self.settings,values=values);budget=TaskBudget(settings)
        opener=Mock(side_effect=[URLError('offline'),response({'ok':True})])
        client=DeepSeekClient(settings,api_key='test-only',opener=opener,sleeper=lambda _:None)
        result=StructuredLLM(client,settings,budget).call('tag_resolver',{}, {'type':'object'})
        self.assertEqual(result,{'ok':True});self.assertEqual(opener.call_count,2);self.assertEqual(budget.calls,2)

    def test_dialogue_transport_retries_obey_its_own_turn_budget(self):
        values=copy.deepcopy(self.settings.values);values['dialogue']['max_llm_calls']=1
        settings=replace(self.settings,values=values)
        opener=Mock(side_effect=[URLError('offline'),response({})])
        client=DeepSeekClient(settings,api_key='test-only',opener=opener,sleeper=lambda _:None)
        result=DialogueAgent(self.manager,client,settings).chat('Покажи мои условия')
        self.assertEqual(opener.call_count,1)
        self.assertIn('лимит',result['answer'])
        self.assertEqual(result['llm_calls'],1)

    def test_explicit_region_change_requires_clarification_without_saving(self):
        self.manager.update_context({'locations':['Россия']},authorized=True)
        before=self.manager.get_state();original=self.client.complete
        def foreign_region(messages,**kwargs):
            result=original(messages,**kwargs)
            if 'планирования' in messages[0]['content']:
                value=json.loads(result['content']);value['search_queries']['queries'][0]['areas']=['Казахстан']
                value['geography_change']={'locations':['Казахстан'],'evidence':'в Казахстане'}
                result['content']=json.dumps(value)
            return result
        self.client.complete=foreign_region
        report=self.search('Разово найди вакансии в Казахстане')
        self.assertEqual(self.source.requests,[])
        self.assertIn('LOCATION_CLARIFICATION',report['stop_reason'])
        self.assertIn('сохранённой географии',report['summary'])
        self.assertEqual(self.manager.get_state(),before)

    def test_optional_positive_config_values_reject_explicit_null(self):
        source=json.loads((ROOT/'config/v0.1.json').read_text())
        cases=[('search','max_llm_calls'),('search','target_count'),('llm','max_tokens'),
               ('dialogue','max_llm_calls'),('source','content_timeout_seconds'),
               ('hh','manual_action_timeout_seconds'),('diagnostics','max_file_bytes'),
               ('diagnostics','max_response_chars'),('diagnostics','retention_days')]
        for section,key in cases:
            with self.subTest(key=key):
                value=copy.deepcopy(source);value[section][key]=None
                path=self.root/'invalid.json';path.write_text(json.dumps(value))
                with self.assertRaisesRegex(ValueError,key):load_settings(path)


if __name__=='__main__':unittest.main()
