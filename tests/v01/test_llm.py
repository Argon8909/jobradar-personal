import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from jobradar.config import load_settings
from jobradar.llm.client import DeepSeekClient, LLMError, read_api_key

ROOT = Path(__file__).resolve().parents[2]


def response(message=None, finish="stop"):
    return io.BytesIO(json.dumps({"choices":[{"message":message or {"role":"assistant","content":"Ответ"},
                                             "finish_reason":finish}]}).encode())


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.settings = load_settings(ROOT/"config/v0.1.json")

    def client(self, opener):
        return DeepSeekClient(self.settings, api_key="private-test-key", opener=opener, sleeper=lambda _:None)

    def test_sends_tools_timeout_and_json_mode(self):
        opener = Mock(return_value=response())
        self.client(opener).complete([{"role":"user","content":"Привет"}],tools=[{"type":"function"}],json_mode=True)
        req = opener.call_args.args[0]
        body = json.loads(req.data)
        self.assertEqual(body["response_format"], {"type":"json_object"})
        self.assertEqual(body["tools"], [{"type":"function"}])
        self.assertEqual(opener.call_args.kwargs["timeout"], 60)
        self.assertEqual(req.get_header("Authorization"), "Bearer private-test-key")

    def test_transient_error_has_two_retries(self):
        opener = Mock(side_effect=[URLError("private-test-key")]*3)
        with self.assertRaises(LLMError) as result:
            self.client(opener).complete([])
        self.assertEqual(opener.call_count, 3)
        self.assertTrue(result.exception.retryable)
        self.assertNotIn("private-test-key", str(result.exception))

    def test_retry_can_recover(self):
        opener = Mock(side_effect=[URLError("temporary"),response()])
        self.assertEqual(self.client(opener).complete([])["content"], "Ответ")

    def test_unauthorized_http_is_not_retried_or_exposed(self):
        opener = Mock(side_effect=HTTPError("url",401,"private-test-key",{},None))
        with self.assertRaises(LLMError) as result:
            self.client(opener).complete([])
        self.assertEqual(opener.call_count, 1)
        self.assertNotIn("private-test-key", str(result.exception))

    def test_rate_limit_retried(self):
        opener = Mock(side_effect=[HTTPError("url",429,"secret",{},None),response()])
        self.client(opener).complete([])
        self.assertEqual(opener.call_count, 2)

    def test_truncated_generation_never_executes_partial_calls(self):
        opener = Mock(return_value=response(finish="length"))
        with self.assertRaises(LLMError):
            self.client(opener).complete([])
        self.assertEqual(opener.call_count, 1)

    def test_invalid_protocol_rejected(self):
        for payload in (b"{}",b"broken",b'{"choices":[]}',b'{"choices":[{"message":{"role":"user","content":"bad"}}]}'):
            with self.subTest(payload=payload), self.assertRaises(LLMError):
                self.client(Mock(return_value=io.BytesIO(payload))).complete([])

    def test_env_key_priority_and_bom_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/".env"
            path.write_text("\ufeffDEEPSEEK_API_KEY='file-key'\n",encoding="utf-8")
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"env-key"}):
                self.assertEqual(read_api_key(path), "env-key")
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY":""}):
                self.assertEqual(read_api_key(path), "file-key")


if __name__ == "__main__":
    unittest.main()
