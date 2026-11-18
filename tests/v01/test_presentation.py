import unittest

from jobradar.dialogue.presentation import describe, short_confirmation


class PresentationTests(unittest.TestCase):
    def test_profile_summary_is_readable_and_does_not_dump_wire_format(self):
        message = describe("create_profile",{"profession":"Системный аналитик","profile_data":{
            "competencies":["SQL","REST API",r"draw\.io"],
            "experience":[{"domain":"Интеграции","years":1.42},{"domain":"Проектирование API","years":1.75}],
            "education":[],"qualifications":[],
            "additional_attributes":[{"key":"tools","value":"Postman","description":"Инструменты"}]}})
        self.assertIn("Создать профиль «Системный аналитик»",message)
        self.assertIn("1 год 5 месяцев",message)
        self.assertIn("1 год 9 месяцев",message)
        self.assertIn("draw.io",message)
        self.assertIn("Инструменты: Postman",message)
        for marker in ('"profession"','"profile_data"',"competencies","additional_attributes","{","}"):
            self.assertNotIn(marker,message)
        self.assertNotIn("1.42",message)

    def test_criteria_and_clearing_values_are_explicit(self):
        message = describe("update_context",{"changes":{"work_formats":["remote"],"salary":{"min":200000,"currency":"RUB","strict":False},"locations":[]}})
        self.assertIn("удалённая работа",message)
        self.assertIn("200 000 RUB",message)
        self.assertIn("мягкое пожелание",message)
        self.assertIn("География: не ограничено",message)
        self.assertIn("снять",describe("update_context",{"changes":{"salary":None}}))

    def test_conditional_or_question_reply_is_not_a_short_confirmation(self):
        for text in ("Да, но измени зарплату","да нет","да?","не подтверждаю","да и ещё создай другой профиль"):
            self.assertFalse(short_confirmation(text),text)


if __name__ == "__main__":
    unittest.main()
