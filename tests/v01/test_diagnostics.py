"""Raw generation capture, isolation and semantic geography boundaries."""

import copy
import json
import os
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from jobradar.analysis.modules import StructuredLLM
from jobradar.config import load_settings
from jobradar.events import collect
from jobradar.search.budget import TaskBudget
from jobradar.search.service import SearchService
from jobradar.search.trace import TaskTrace
from jobradar.storage import Database
from jobradar.profiles import ProfileManager
from test_search import FixtureLLM, FixtureSource, raw

ROOT=Path(__file__).resolve().parents[2]


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Database(Path(self.temp.name)/"test.sqlite3");self.db.initialize()
        self.settings=load_settings(ROOT/"config/v0.1.json")

    def tearDown(self):
        self.temp.cleanup()

    def test_failed_original_and_successful_repair_are_captured_with_schema_and_location(self):
        class Client:
            replies=iter(['{"value":','{"value":12}'])
            def complete(inner,*args,**kwargs):
                return {"content":next(inner.replies),"finish_reason":"stop","usage":{"total_tokens":10}}
        trace=TaskTrace(self.db,1,self.settings)
        llm=StructuredLLM(Client(),self.settings,TaskBudget(self.settings),observer=trace.issue,recorder=trace)
        schema={"type":"object","properties":{"value":{"type":"integer"}},"required":["value"]}
        result=llm.call("vacancy_analyzer",{"mode":"test","password":"do-not-record"},schema)
        self.assertEqual(result,{"value":12})
        saved=json.loads(trace.path.read_text())
        self.assertEqual([c["status"] for c in saved["llm_calls"]],["error","success"])
        first,second=saved["llm_calls"]
        self.assertEqual(first["response"],'{"value":')
        self.assertEqual(first["error"]["details"]["colno"],10)
        self.assertEqual(first["schema"],schema)
        self.assertEqual(second["response"],'{"value":12}')
        self.assertTrue(second["is_repair"])
        self.assertNotIn("do-not-record",trace.path.read_text())
        self.assertIn("[REDACTED]",trace.path.read_text())

    def test_capture_off_keeps_metadata_without_raw_response(self):
        self.settings.values["diagnostics"]["capture_llm"]="off"
        trace=TaskTrace(self.db,1,self.settings)
        trace.llm_call(prompt_name="test",messages=[{"content":"prompt"},{"content":"private-input"}],schema={},model="test",response={"content":"private-output"},attempt=0,elapsed_ms=12,error={"code":"test"})
        text=trace.path.read_text()
        self.assertNotIn("private-",text)
        self.assertEqual(json.loads(text)["llm_calls"][0]["elapsed_ms"],12)

    def test_payload_limits_and_retention_are_applied(self):
        self.settings.values["diagnostics"].update(capture_llm="all",max_response_chars=100,max_file_bytes=1500)
        trace=TaskTrace(self.db,2,self.settings)
        trace.path.parent.mkdir()
        old=trace.path.parent/"task-1.json";old.write_text("{}")
        os.utime(old,(time.time()-9*86400,)*2)
        trace.prune();self.assertFalse(old.exists())
        trace.llm_call(prompt_name="test",messages=[{"content":"p"},{"content":"input"}],schema={},model="test",response={"content":"x"*500},attempt=0,elapsed_ms=10)
        self.assertTrue(json.loads(trace.path.read_text())["llm_calls"][0]["response_truncated"])
        self.assertLessEqual(trace.path.stat().st_size,1500)

    def search_with_locations(self, resolutions=None):
        manager=ProfileManager.start_session(self.db)
        manager.create_profile("Аналитик",authorized=True)
        manager.update_context({"locations":["ближнее зарубежье"]},authorized=True)
        client=FixtureLLM();source=FixtureSource([raw()]);original=client.complete
        def complete(messages,**kwargs):
            response=original(messages,**kwargs)
            if "планирования" in messages[0]["content"] and resolutions:
                body=json.loads(response["content"]);body["resolved_locations"]=resolutions
                response["content"]=json.dumps(body)
            return response
        client.complete=complete
        service=SearchService(self.db,self.settings,client,{"hh":source})
        before=manager.get_state()
        result=collect(service.search(manager,{},before))
        self.assertEqual(manager.get_state(),before)
        return result,source

    def test_group_is_expanded_for_source_but_saved_criteria_are_not_overwritten(self):
        result,source=self.search_with_locations([{"input":"ближнее зарубежье","areas":["Беларусь","Казахстан"],"explanation":"Выбранные пользователем страны."}])
        self.assertEqual(source.requests[0]["areas"],["Беларусь","Казахстан"])
        self.assertNotIn("ближнее зарубежье",source.requests[0]["text"])
        self.assertTrue(any(i["code"]=="LOCATION_RESOLVED" for i in result["diagnostics"]["issues"]))

    def test_unresolved_group_asks_human_question_before_source_access(self):
        result,source=self.search_with_locations()
        self.assertEqual(source.requests,[])
        self.assertIn("Какие страны",result["summary"])
        self.assertNotIn("LOCATION_CLARIFICATION",result["summary"])

    def test_failed_write_does_not_break_generation(self):
        trace=TaskTrace(self.db,1,self.settings)
        with patch("pathlib.Path.replace",side_effect=OSError("disk full")):
            trace.issue({"code":"TEST","message":"failure","stage":"test","severity":"error"})
        self.assertEqual(trace.data["issues"][0]["code"],"TEST")
