"""Stage 3: real SQLite and services, scripted source/model, no external calls."""
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'tests/v01'))
from test_search import FixtureLLM, FixtureSource, raw
from test_dialogue import FakeClient, intent, calls, answer
from jobradar.config import load_settings
from jobradar.dialogue.agent import DialogueAgent
from jobradar.events import collect
from jobradar.profiles import ProfileManager, ProfileError
from jobradar.search.period import (publication_date, resolve_period, date_state, stored_period,
                                     assessment_criteria, availability_observed, MOSCOW)
from jobradar.search.service import SearchService
from jobradar.search.trace import search_explanation
from jobradar.storage import Database, vacancies, search as tasks
from jobradar.tools.dispatcher import ToolDispatcher, canonical
from jobradar.tools.registry import ToolRegistry

START = '2026-03-31T12:34:56+03:00'


def restriction(value):
    return {'attribute':'publication_period','operator':'within','value':value}


class PeriodMathTests(unittest.TestCase):
    def test_inclusive_calendar_boundary_and_timezones(self):
        p=resolve_period({}, {'vacancy_max_age_days':30},START)
        self.assertEqual(p['cutoff'],'2026-02-28T21:00:00+00:00')
        for value,state in [('2026-03-01','included'),('2026-02-28T20:59:59Z','excluded'),
                            ('2026-03-01T00:00:00+03:00','included'),('bad','unknown'),(None,'unknown'),
                            ('2026-04-01','unknown')]:
            with self.subTest(value=value):self.assertEqual(date_state({'published_at':value},p),state)

    def test_month_end_and_leap_year_calendar_subtraction(self):
        for start,months,expected in [('2024-03-31',1,'2024-02-29'),('2025-03-31',1,'2025-02-28'),
                                       ('2026-01-31',2,'2025-11-30'),('2026-03-31',2,'2026-01-31')]:
            p=resolve_period({}, {},start,{'unit':'months','value':months})
            self.assertEqual(publication_date(p['cutoff']).astimezone(MOSCOW).date().isoformat(),expected)

    def test_priority_disable_and_reset(self):
        c={'restrictions':[restriction('weeks:2')]};limits={'vacancy_max_age_days':30}
        self.assertEqual(resolve_period(c,limits,START)['source'],'context')
        p=resolve_period(c,limits,START,{'unit':'unlimited','value':None})
        self.assertEqual((p['source'],p['cutoff']),('request',None))
        p=resolve_period(c,limits,START,{'unit':'default','value':None})
        self.assertEqual((p['value'],p['source'],p['reset_to_default']),(30,'request',True))
        self.assertEqual(resolve_period({'restrictions':[restriction('default')]},limits,START)['value'],30)
        self.assertEqual(resolve_period({},limits,START)['source'],'configuration')

    def test_status_checked_in_later_task_is_not_a_check_during_old_search(self):
        period=resolve_period({}, {},START)
        raw={'status':'available','last_checked_at':'2026-04-02T00:00:00Z'}
        self.assertFalse(availability_observed(raw,period,'2026-03-31T13:00:00+03:00'))
        raw['last_checked_at']='2026-03-31T12:40:00+03:00'
        self.assertTrue(availability_observed(raw,period,'2026-03-31T13:00:00+03:00'))

    def test_missing_or_bad_dates_never_use_download_time(self):
        p=resolve_period({}, {'vacancy_max_age_days':30},START)
        self.assertEqual(date_state({'published_at':None,'fetched_at':START},p),'unknown')
        self.assertEqual(date_state({'published_at':'2025-01-01','fetched_at':START},p),'excluded')


class Stage3SearchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Database(Path(self.temp.name)/'test.sqlite3');self.db.initialize()
        self.manager=ProfileManager.start_session(self.db)
        self.manager.create_profile('Аналитик',{'competencies':['SQL']},authorized=True)
        self.settings=load_settings(ROOT/'config/v0.2.json')
        self.client,self.source=FixtureLLM(),FixtureSource([])
        self.override=None
        original=self.client.complete
        def complete(messages,**kw):
            response=original(messages,**kw)
            if 'планирования' in messages[0]['content'] and self.override:
                data=json.loads(response['content']);data['publication_period']=self.override
                response['content']=json.dumps(data)
            return response
        self.client.complete=complete

    def tearDown(self):self.temp.cleanup()

    def service(self):return SearchService(self.db,self.settings,self.client,{'hh':self.source})

    def search(self,request=None):
        with patch('jobradar.search.trace.datetime') as dt:
            # A fixed real datetime value, retaining all normal parsing elsewhere.
            dt.now.return_value=publication_date(START)
            return collect(self.service().search(self.manager,{'request':request} if request else {},self.manager.get_state()))

    def store(self,*items):
        with self.db.transaction() as db:return [vacancies.upsert_raw(db,item) for item in items]

    def override_period(self,unit,value,evidence):self.override={'unit':unit,'value':value,'evidence':evidence}

    def test_default_excludes_old_local_and_new_before_paid_analysis(self):
        self.store(replace(raw('1'),published_at='2026-02-28'))
        self.source.items=[replace(raw('2'),published_at='2026-02-28')]
        r=self.search()
        self.assertEqual(self.client.count('extract_and_assess'),0)
        self.assertEqual(r['vacancies'],[])
        self.assertEqual(r['eligibility_counts']['age_excluded'],2)
        self.assertEqual(r['diagnostics']['metrics']['age_excluded'],2)
        with self.db.connection() as db:self.assertEqual(db.execute('select count(*) from vacancies').fetchone()[0],2)

    def test_boundary_and_unknown_dates_are_processed(self):
        self.store(replace(raw('1'),published_at='2026-03-01'),replace(raw('2'),published_at=None),
                   replace(raw('3'),published_at='bad'))
        r=self.search();self.assertEqual(r['result_count'],3)
        self.assertEqual(r['eligibility_counts']['unknown_publication_date'],2)
        self.assertEqual(r['publication_period']['source'],'configuration')

    def test_current_cards_are_age_filtered_without_reassessment(self):
        self.store(replace(raw('1'),published_at='2026-02-01'))
        self.override_period('unlimited',None,'без ограничения')
        self.search('Ищи без ограничения')
        self.override=None;self.client.calls.clear()
        r=self.search();self.assertEqual(r['vacancies'],[])
        self.assertEqual(self.client.count('extract_and_assess'),0)
        self.assertEqual(self.client.count('assessment_only'),0)

    def test_one_off_expansion_restores_old_records_and_preserves_revision_and_cache(self):
        self.store(replace(raw('1'),published_at='2026-02-01'))
        state=self.manager.get_state();self.assertEqual(self.search()['vacancies'],[])
        self.override_period('months',2,'два месяца');r=self.search('Покажи за последние два месяца')
        self.assertEqual(r['result_count'],1)
        self.assertEqual(r['publication_period']['source'],'request')
        self.assertEqual(self.manager.get_state(),state)
        self.override=None;self.assertEqual(self.search()['vacancies'],[])
        self.override_period('months',2,'два месяца');self.client.calls.clear()
        self.assertEqual(self.search('Покажи за последние два месяца')['result_count'],1)
        self.assertEqual(self.client.count('extract_and_assess'),0)
        self.assertEqual(self.client.count('assessment_only'),0)

    def test_one_off_week_disable_and_default_override_context_without_revision(self):
        self.manager.update_context({'restrictions':[restriction('months:2')]},authorized=True)
        self.store(replace(raw('1'),published_at='2026-03-10'))
        before=self.manager.get_state()
        for unit,value,phrase,count in [('weeks',1,'не старше недели',0),('unlimited',None,'без ограничения по дате',1),('default',None,'стандартный период поиска',1)]:
            self.override_period(unit,value,phrase)
            r=self.search('Ищи '+phrase)
            self.assertEqual(r['result_count'],count)
            self.assertEqual(r['publication_period']['source'],'request')
            self.assertEqual(self.manager.get_state(),before)
        self.override=None
        self.assertEqual(self.search()['publication_period']['source'],'context')

    def test_persisted_period_does_not_enter_personal_assessment(self):
        self.manager.update_context({'restrictions':[restriction('months:2')]},authorized=True)
        self.store(replace(raw('1'),published_at='2026-02-01'))
        r=self.search();self.assertEqual(r['result_count'],1)
        data=[json.loads(m[1]['content'])['input'] for m,k in self.client.calls]
        data=next(d for d in data if d.get('mode')=='extract_and_assess')
        self.assertEqual(data['criteria']['restrictions'],[])
        self.assertEqual(r['publication_period']['source'],'context')

    def test_unknown_availability_not_claimed_open_from_old_status(self):
        self.store(raw('1'))
        with self.db.transaction() as db:db.execute("update vacancies set last_checked_at='2025-01-01'")
        r=self.search()
        self.assertEqual(r['eligibility_counts']['availability_unconfirmed'],1)
        self.assertEqual(r['vacancies'][0]['availability'],'unconfirmed')
        self.assertIn('повторно не проверялась',r['summary'])

    def test_disabled_age_still_excludes_confirmed_closed(self):
        self.store(replace(raw('1'),published_at='2020-01-01'))
        with self.db.transaction() as db:vacancies.mark_closed(db,'hh','1')
        self.override_period('unlimited',None,'без ограничения')
        r=self.search('Ищи без ограничения');self.assertEqual(r['vacancies'],[])
        self.assertEqual(self.client.count('extract_and_assess'),0)

    def test_new_date_on_unchanged_source_preserves_card_and_assessment(self):
        self.store(raw('1'));self.search();self.client.calls.clear()
        with self.db.connection() as db:
            before=vacancies.get_raw(db,1);card=vacancies.get_card(db,1);score=vacancies.get_assessment(db,1,1)
        self.source.items=[replace(raw('1'),published_at='2026-03-15')]
        self.search()
        with self.db.connection() as db:
            self.assertEqual(vacancies.get_raw(db,1)['content_hash'],before['content_hash'])
            self.assertEqual(vacancies.get_card(db,1),card)
            self.assertEqual(vacancies.get_assessment(db,1,1),score)
        self.assertEqual(self.client.count('extract_and_assess'),0)
        self.assertEqual(self.client.count('assessment_only'),0)

    def test_legacy_hash_kept_on_date_change_but_content_change_invalidates(self):
        self.store(raw('1'));self.search()
        legacy={key:getattr(raw('1'),key) for key in ('source','external_id','url','title','employer','description','published_at')}
        digest=hashlib.sha256(vacancies.encode(legacy).encode()).hexdigest()
        with self.db.transaction() as db:
            db.execute('update vacancies set content_hash=?',(digest,));db.execute('update vacancy_cards set source_hash=?',(digest,))
        self.store(replace(raw('1'),published_at='2026-03-20'))
        with self.db.connection() as db:self.assertTrue(vacancies.card_current(vacancies.get_raw(db,1),vacancies.get_card(db,1)))
        self.store(replace(raw('1'),description='Другие задачи SQL и PostgreSQL',published_at='2026-03-20'))
        with self.db.connection() as db:self.assertFalse(vacancies.card_current(vacancies.get_raw(db,1),vacancies.get_card(db,1)))

    def test_date_update_during_search_can_revive_previously_old_raw(self):
        self.store(replace(raw('1'),published_at='2026-01-01'))
        self.source.items=[replace(raw('1'),published_at='2026-03-20')]
        r=self.search();self.assertEqual(r['result_count'],1)
        self.assertEqual(r['eligibility_counts']['age_excluded'],0)

    def test_results_and_refresh_apply_original_task_period_after_restart(self):
        self.store(replace(raw('1'),published_at='2026-02-01'));self.search()
        self.override_period('unlimited',None,'без ограничения');r=self.search('Ищи без ограничения')
        self.override=None;self.client.calls.clear()
        old=collect(self.service().get_results(self.manager,{'task_id':1},self.manager.get_state()))
        new=collect(self.service().get_results(self.manager,{'task_id':r['task_id']},self.manager.get_state()))
        self.assertEqual(old['vacancies'],[]);self.assertEqual(new['result_count'],1)
        self.assertEqual(self.client.count('assessment_only'),0)

    def test_unconfirmed_period_and_numeric_region_are_rejected_before_hh(self):
        self.override_period('weeks',1,'несуществующая цитата');r=self.search('Ищи работу')
        self.assertIn('PERIOD_CLARIFICATION',r['stop_reason']);self.assertEqual(self.source.requests,[])
        self.override=None;self.manager.update_context({'locations':['113']},authorized=True)
        r=self.search();self.assertIn('LOCATION_CLARIFICATION',r['stop_reason']);self.assertEqual(self.source.requests,[])

    def test_explanation_uses_recorded_facts_without_starting_search(self):
        self.store(replace(raw('1'),published_at='2020-01-01'));self.search()
        registry=ToolRegistry();self.service().register(registry)
        agent=DialogueAgent(self.manager,FakeClient(intent(explain_search=True)),self.settings,ToolDispatcher(self.manager,registry))
        revision=self.manager.get_state();result=agent.chat('Почему так мало вакансий?')
        self.assertIn('Отфильтровано: 1',result['answer']);self.assertIn('Аналитик SQL',result['answer'])
        self.assertIn('готовые карточки',result['answer']);self.assertEqual(result['calls'],[])
        self.assertEqual(self.manager.get_state(),revision)
        self.assertNotIn('RESULTS_EXHAUSTED',result['answer'])


class Stage3ConfigDialogueTests(unittest.TestCase):
    def test_v01_and_v02_explicit_config_paths_and_single_search_source(self):
        old,new=load_settings(ROOT/'config/v0.1.json'),load_settings(ROOT/'config/v0.2.json')
        self.assertNotIn('vacancy_max_age_days',old.values['search'])
        self.assertEqual(new.values['search']['vacancy_max_age_days'],30)
        self.assertEqual(new.database_path,old.database_path);self.assertEqual(new.prompts_dir,old.prompts_dir)
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);v=json.loads((ROOT/'config/v0.2.json').read_text());v['search_config']='limits.json'
            p=root/'main.json';p.write_text(json.dumps(v));limits=dict(new.values['search'])
            for key,bad in [('max_llm_calls',None),('max_source_vacancies',True),('target_count',0),('vacancy_max_age_days',-1),('vacancy_max_age_days',False)]:
                with self.subTest(key=key,value=bad):
                    (root/'limits.json').write_text(json.dumps({**limits,key:bad}))
                    with self.assertRaises(ValueError):load_settings(p)
            (root/'limits.json').write_text(json.dumps({**limits,'vacancy_max_age_days':None,'max_source_vacancies':500}))
            loaded=load_settings(p);self.assertEqual(loaded.values['search']['max_source_vacancies'],500)
            v['search']={};p.write_text(json.dumps(v))
            with self.assertRaisesRegex(ValueError,'not both'):load_settings(p)

    def test_persistent_period_dialogue_preserves_other_constraints_and_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Database(Path(temp)/'test.sqlite3');db.initialize();manager=ProfileManager.start_session(db)
            manager.create_profile('Аналитик',authorized=True)
            other={'attribute':'domain','operator':'exclude','value':'Азартные игры'}
            manager.update_context({'locations':['Москва'],'work_formats':['remote'],'restrictions':[other]},authorized=True)
            before=manager.get_state();args={'changes':{'restrictions':[restriction('days:14')]}}
            text='В дальнейшем рассматривай вакансии только за последние 14 дней'
            client=FakeClient(intent([('update_context',args)],evidence=text),calls(('1','update_context',args)),answer())
            agent=DialogueAgent(manager,client,load_settings(ROOT/'config/v0.2.json'));r=agent.chat(text)
            after=manager.get_state();self.assertEqual(after['profile'],before['profile'])
            self.assertEqual(after['context']['criteria_revision'],before['context']['criteria_revision']+1)
            self.assertEqual(after['context']['criteria']['locations'],['Москва'])
            self.assertEqual(after['context']['criteria']['restrictions'],[other,restriction('days:14')])
            self.assertIn('14 календарных дней',r['answer'])
            for value in ['unlimited','default']:
                manager.update_context({'restrictions':[restriction(value)]},authorized=True)
                self.assertEqual(stored_period(manager.get_state()['context']['criteria'])['unit'],value)
            for value in ['days:0','months:1.5','oops']:
                with self.assertRaises(ProfileError):manager.update_context({'restrictions':[restriction(value)]},authorized=True)

    def test_period_proposal_requires_confirmation_and_question_cancels_stale_yes(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Database(Path(temp)/'test.sqlite3');db.initialize();m=ProfileManager.start_session(db)
            m.create_profile('Аналитик',authorized=True);before=m.get_state()
            args={'changes':{'restrictions':[restriction('weeks:1')]}}
            settings=load_settings(ROOT/'config/v0.2.json')
            client=FakeClient(intent(),calls(('1','update_context',args)),answer())
            agent=DialogueAgent(m,client,settings);result=agent.chat('Что посоветуешь?')
            self.assertIn('Подтверждаете?',result['answer']);self.assertEqual(m.get_state(),before)
            result=agent.chat('да');self.assertEqual(stored_period(m.get_state()['context']['criteria']),{'unit':'weeks','value':1})
            # A concrete multi-field context proposal must not be silently reduced
            # or executed after a question instead of consent.
            proposal={'name':'create_context','arguments':{'name':'AI','criteria':{'target_roles':['AI-автоматизация'],'work_formats':['remote']}},'state':m.get_state()}
            agent.pending=copy.deepcopy(proposal)
            client.responses += [intent(explain_search=True)]
            before=m.get_state();agent.chat('Почему ищешь только системных аналитиков?')
            self.assertEqual(m.get_state(),before);self.assertIsNone(agent.pending)
            client.responses += [intent(),answer('Уточните действие.')]
            agent.chat('да');self.assertEqual(len(m.list_contexts()['contexts']),1)
            # An explicit current proposal executes exactly its complete arguments.
            agent.pending=copy.deepcopy(proposal);agent.chat('да')
            self.assertEqual(m.get_state()['context']['criteria']['work_formats'],['remote'])
            self.assertEqual(m.get_state()['context']['criteria']['target_roles'],['AI-автоматизация'])

    def test_adjacent_one_off_search_keeps_existing_profile_and_explicit_role_request(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Database(Path(temp)/'test.sqlite3');db.initialize();m=ProfileManager.start_session(db)
            m.create_profile('Системный аналитик',{'competencies':['REST API','SQL']},authorized=True)
            m.update_context({'locations':['Россия'],'work_formats':['remote']},authorized=True)
            registry=ToolRegistry();received=[]
            registry.register('search_vacancies',lambda manager,args,state:received.append((args,state)) or {'summary':'Поиск выполнен'})
            for request in ['Найди также работу с интеграциями и AI-автоматизацией','Ищи только системных аналитиков, смежные роли не нужны']:
                before=m.get_state();args={'request':request}
                client=FakeClient(intent([('search_vacancies',args)],evidence=request),calls(('1','search_vacancies',args)),answer('Готово'))
                agent=DialogueAgent(m,client,load_settings(ROOT/'config/v0.2.json'),ToolDispatcher(m,registry))
                result=agent.chat(request)
                self.assertEqual(m.get_state(),before);self.assertEqual(received[-1][0]['request'],request)
                self.assertNotIn('Подтверждаете',result['answer'])
                self.assertEqual(len(m.list_profiles()['profiles']),1);self.assertEqual(len(m.list_contexts()['contexts']),1)

    def test_current_request_text_not_model_summary_reaches_search(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Database(Path(temp)/'test.sqlite3');db.initialize();m=ProfileManager.start_session(db);m.create_profile('Аналитик',authorized=True)
            registry=ToolRegistry();received=[]
            registry.register('search_vacancies',lambda manager,args,state:received.append(args) or {})
            d=ToolDispatcher(m,registry);args={'request':'Ищи за два месяца из старой истории'}
            before=m.get_state();r=d.execute('search_vacancies',args,expected_state=before,grant=canonical('search_vacancies',args),request_text='Ищи не старше недели')
            self.assertEqual(r['status'],'success');self.assertEqual(received[0]['request'],'Ищи не старше недели');self.assertEqual(m.get_state(),before)
