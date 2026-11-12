import unittest
import collector
import deepseek

class CollectorRegression(unittest.TestCase):
    def test_preserve_author_repetition(self):
        raw='Работа с API\n\n\nРабота с API\n  \nЗадайте вопрос работодателю\nДругой вопрос\nПоказать на большой карте'
        cleaned=collector.clean_description(raw)
        self.assertEqual(cleaned.count('Работа с API'),2)
        self.assertNotIn('Задайте вопрос',cleaned)
        self.assertNotIn('\n\n\n',cleaned)

    def test_browser_profile_unchanged(self):
        self.assertEqual(collector.PROFILE_DIR.name,'browser_profile')
        self.assertEqual(collector.OUTPUT.name,'vacancies.json')

    def test_provider_and_model(self):
        cfg=deepseek.read_config()
        self.assertEqual(cfg['ai']['provider'],'deepseek')
        self.assertEqual(cfg['ai']['model'],'deepseek-flash')

if __name__=='__main__':unittest.main()
