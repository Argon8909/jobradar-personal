"""Offline regressions: no live DeepSeek, no HH login, no browser."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import orchestrator
import storage


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.old=storage.DB
        storage.DB=Path(self.temp.name)/'test.db'
        storage.init()

    def tearDown(self):
        storage.DB=self.old
        self.temp.cleanup()

    def test_profile_update_and_remove(self):
        p=orchestrator.do_tool('update_profile',{'skills':[{'name':'REST API','level':'практический опыт'}]})
        self.assertEqual(p['skills'][0]['name'],'REST API')
        p=orchestrator.do_tool('update_profile',{'skills':[{'name':'rest api','level':'базовый'}]})
        self.assertEqual(len(p['skills']),1)
        result=orchestrator.do_tool('remove_skill',{'name':'REST API'})
        self.assertTrue(result['removed'])
        self.assertEqual(storage.get_profile()['skills'],[])

    def test_confirmation_clear(self):
        storage.upsert_vacancies([{'id':'abc','title':'Analyst'}])
        req=orchestrator.do_tool('request_clear_database',{})
        self.assertTrue(req['confirmation_required'])
        self.assertEqual(storage.counts()['vacancies'],1)
        self.assertIsNone(orchestrator._pending_response('А почему?'))
        self.assertEqual(storage.counts()['vacancies'],1)
        self.assertIn('удалены',orchestrator._pending_response('Да, подтверждаю'))
        self.assertEqual(storage.counts()['vacancies'],0)

    def test_tool_calls_chat(self):
        calls=[{'id':'call1','type':'function','function':{'name':'update_profile','arguments':json.dumps({'skills':[{'name':'Kafka','level':'начальный'}]})}}]
        mocked=[({'role':'assistant','content':None,'tool_calls':calls},{'total_tokens':15}),
                ({'role':'assistant','content':'Записал Kafka в профиль.'},{'total_tokens':8})]
        with patch.object(orchestrator.deepseek,'completion',side_effect=mocked) as m:
            result=orchestrator.chat('Kafka знаю немного')
            self.assertIn('Записал',result['answer'])
            self.assertEqual(result['usage']['total_tokens'],23)
            self.assertEqual(storage.get_profile()['skills'][0]['name'],'Kafka')
            second=m.call_args_list[1].args[0]
            self.assertTrue(any(x.get('role')=='tool' for x in second))

    def test_background_search_and_status(self):
        vacancy={'id':'123','title':'SA','url':'https://hh.ru/vacancy/123','description':'REST API'}
        with patch.object(orchestrator.collector,'collect_vacancies',return_value=[vacancy]), \
             patch.object(orchestrator.deepseek,'classify_and_analyze',return_value=({'profession':'Системный аналитик','summary':'REST','specialization':[],'skills':['REST'],'certificates':[],'conditions':[],'required':[],'preferred':[]},{'category':'подходит сейчас','reason':'Да','gaps':[]},{'total_tokens':9})), \
             patch.object(orchestrator,'config_limits',return_value={'max_queries':1,'max_vacancies':2,'per_query':1}):
            result=orchestrator.start_search('системный аналитик AI',['аналитик AI'])
            self.assertTrue(result['ok'])
            orchestrator._worker.join(timeout=3)
        self.assertFalse(orchestrator._worker.is_alive())
        self.assertEqual(storage.counts(),{'vacancies':1,'analyses':1})
        self.assertEqual(storage.last_task()['state'],'completed')
        self.assertIn('https://hh.ru/vacancy/123',storage.messages()[-1]['content'])
        self.assertIn('Результаты задачи',storage.messages()[-1]['content'])
        self.assertEqual(storage.get_card('123')['profession'],'Системный аналитик')


if __name__=='__main__':
    unittest.main()

class SearchV22Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.old=storage.DB
        storage.DB=Path(self.temp.name)/'test.db'
        storage.init()

    def tearDown(self):
        storage.DB=self.old
        self.temp.cleanup()

    def test_tags_search_without_api(self):
        vacancy={'id':'w1','title':'Монтажник','url':'https://hh.ru/vacancy/w1','description':'Сварочные работы'}
        storage.upsert_vacancies([vacancy])
        storage.save_card('w1',{'profession':'Сварщик','summary':'Монтаж на высоте',
              'specialization':[],'skills':[],'certificates':['НАКС'],'conditions':['высотные работы'],
              'required':[],'preferred':[]})
        rows=storage.find_vacancies(['сварщик НАКС'],limit=10)
        self.assertEqual([v['vacancy']['id'] for v in rows],['w1'])
        self.assertEqual(storage.existing_ids(['w1','missing']),{'w1'})

    def test_result_report_when_limit_not_reached(self):
        storage.upsert_vacancies([{'id':'v1','title':'Аналитик','description':'AI LLM','url':'https://hh.ru/vacancy/v1'}])
        storage.save_analysis('v1',{'analysis':{'category':'сомнительная','reason':'Мало информации','gaps':[]}})
        storage.save_card('v1',{'profession':'Аналитик','summary':'AI','specialization':[],'skills':['LLM'],
                               'certificates':[],'conditions':[],'required':[],'preferred':[]})
        taskid=storage.create_task('аналитик LLM',{'target_analyzed':20,'max_queries':1,'max_vacancies':5,
                                                  'per_query':1,'autonomous':False,'queries':['аналитик LLM'],'used':[]})
        with patch.object(orchestrator.collector,'collect_vacancies',return_value=[]):
            orchestrator._search_loop(taskid)
        msg=storage.messages()[-1]['content']
        self.assertIn('из запрошенных 20',msg)
        self.assertIn('https://hh.ru/vacancy/v1',msg)
        self.assertIn('Запрошенное количество не достигнуто',msg)
        self.assertEqual(storage.last_task()['state'],'completed')
