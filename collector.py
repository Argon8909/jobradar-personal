"""Пробный сбор вакансий hh.ru через Playwright с постоянной сессией браузера."""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

from playwright.sync_api import sync_playwright


VACANCY_ID_RE = re.compile(r"/vacancy/(\d+)")
ROOT = Path(__file__).resolve().parent
PROFILE_DIR = ROOT / "browser_profile"  # НЕ публиковать: здесь cookies авторизации
OUTPUT = ROOT / "vacancies.json"


def is_challenge(page):
    address = page.url.lower()
    if any(word in address for word in ("captcha", "challenge")):
        return True
    return page.locator('input[name="captcha"], [data-qa="captcha"], iframe[src*="captcha"]').count() > 0


def collect_links(page, limit):
    links, seen = [], set()
    for href in page.locator('a[href*="/vacancy/"]').evaluate_all(
        "elements => elements.map(el => el.href)"
    ):
        match = VACANCY_ID_RE.search(urlparse(href).path)
        if not match:
            continue
        vacancy_id = match.group(1)
        if vacancy_id not in seen:
            seen.add(vacancy_id)
            links.append((vacancy_id, f"https://hh.ru/vacancy/{vacancy_id}"))
        if len(links) >= limit:
            break
    return links


def get_text(page, selectors):
    for selector in selectors:
        element = page.locator(selector).first
        if element.count():
            try:
                value = element.inner_text(timeout=3000).strip()
                if value:
                    return value
            except Exception:
                continue
    return None


def first_string(value):
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def jobposting_data(page):
    """Дополнительный источник: JobPosting JSON-LD, если он есть на странице."""
    for raw in page.locator('script[type="application/ld+json"]').all_text_contents():
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            continue
        queue = [payload]
        while queue:
            item = queue.pop(0)
            if isinstance(item, list):
                queue.extend(item)
            elif isinstance(item, dict):
                types = item.get('@type', [])
                if isinstance(types, str):
                    types = [types]
                if 'JobPosting' in types:
                    return item
                if '@graph' in item:
                    queue.append(item['@graph'])
    return {}


def location_from_jobposting(job):
    """Извлекаем географию вакансии, а не адрес компании."""
    locations = job.get('jobLocation')
    if isinstance(locations, dict):
        locations = [locations]
    area, address = None, None
    for location in locations or []:
        if not isinstance(location, dict):
            continue
        postal = location.get('address', {})
        if isinstance(postal, str):
            address = first_string(postal)
            continue
        if not isinstance(postal, dict):
            continue
        city = first_string(postal.get('addressLocality'))
        region = first_string(postal.get('addressRegion'))
        country = postal.get('addressCountry')
        if isinstance(country, dict):
            country = country.get('name')
        country = first_string(country)
        area = area or city or region or country
        parts = [first_string(postal.get('streetAddress')), city, region, country]
        parts = [p for i, p in enumerate(parts) if p and p not in parts[:i]]
        if parts:
            address = ', '.join(parts)
        if area:
            break
    return area, address


def explicit_field(text, labels):
    """Берём только явные строки с подписью поля, без предположений по смыслу."""
    if not text:
        return None
    for line in text.splitlines():
        line = ' '.join(line.split())
        for label in labels:
            match = re.match(rf"^{re.escape(label)}\s*[:—–-]\s*(.+)$", line, re.I)
            if match:
                value = match.group(1).strip(' .;')
                if value and len(value) <= 140:
                    return value
    return None


def page_metadata(page):
    """Получаем видимый текст без описания вакансии и без скрытых элементов."""
    return page.evaluate("""() => {
        const bodyText = document.body.innerText || '';
        const desc = document.querySelector('[data-qa="vacancy-description"], .vacancy-description');
        const descText = desc ? desc.innerText : '';
        return descText ? bodyText.replace(descText, '') : bodyText;
    }""")


def qa_metadata(page, phrases):
    """Ищем короткие значения в элементах с data-qa, без привязки к одному селектору."""
    candidates = page.locator('[data-qa]').evaluate_all("""(elements, phrases) =>
        elements.filter(el => phrases.some(p => (el.getAttribute('data-qa') || '').toLowerCase().includes(p)))
            .map(el => (el.innerText || '').trim())
            .filter(text => text.length > 0 && text.length < 160)
    """, phrases)
    for candidate in candidates:
        if candidate:
            return candidate
    return None


def extract_work_details(page, description, job, debug=False):
    """Приоритет: отдельные атрибуты страницы, затем явные подписи полей."""
    work_format = get_text(page, [
        '[data-qa="vacancy-view-work-formats"]',
        '[data-qa="vacancy-work-formats"]',
        '[data-qa="vacancy-view-employment-mode"]',
    ]) or qa_metadata(page, ['work-format', 'work_format', 'employment-mode'])
    schedule = get_text(page, [
        '[data-qa="vacancy-view-work-schedule"]',
        '[data-qa="vacancy-work-schedule"]',
        '[data-qa="vacancy-view-schedule"]',
    ]) or qa_metadata(page, ['work-schedule', 'work_schedule', 'schedule-by-days'])

    metadata = page_metadata(page)
    format_labels = ['Формат работы', 'Формат занятости']
    schedule_labels = ['График работы', 'График', 'Рабочий график', 'Режим работы']
    work_format = work_format or explicit_field(metadata, format_labels)
    schedule = schedule or explicit_field(metadata, schedule_labels)

    # В описании учитываем лишь явные подписи, а не отдельные слова вроде «удалённо».
    work_format = work_format or explicit_field(description, format_labels)
    schedule = schedule or explicit_field(description, schedule_labels)
    if not work_format and job.get('jobLocationType') == 'TELECOMMUTE':
        work_format = 'Удалённо'

    if debug and (not work_format or not schedule):
        print('  Диагностика полей (фрагменты страницы):')
        for line in metadata.splitlines():
            line = ' '.join(line.split())
            if line and len(line) <= 160 and re.search(r'формат|график|удал[её]н|гибрид|офис|сменн|рабочие дни', line, re.I):
                print('   ', line[:160])
    return work_format, schedule


def clean_description(text):
    """Убираем только видимые элементы интерфейса HH и шум пробелов.

    Содержание объявления, в том числе повторы работодателя, не сокращаем.
    """
    if not text:
        return text

    # На некоторых страницах блок вакансии захватывает нижнюю панель HH.
    # Отсекаем её только при наличии характерной комбинации элементов интерфейса.
    footer = re.search(
        r"(?im)^\s*Задайте вопрос работодателю\s*$", text
    )
    if footer:
        tail = text[footer.start():]
        if re.search(r"(?im)^\s*(Другой вопрос|Где предстоит работать|Показать на\s+большой карте)\s*$", tail):
            head = text[:footer.start()]
            # В этом случае и «Контакты / ФИО / Связаться» — часть интерфейса HH.
            head = re.sub(
                r"(?im)\n\s*Контакты\s*\n[^\n]{1,140}\n\s*Связаться\s*$",
                "", head.rstrip()
            )
            text = head

    # Нормализация пробелов, без объединения строк и удаления повторов.
    text = text.replace("\u00a0", " ").replace("\u202f", " ").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    compact = []
    for line in lines:
        if line or (compact and compact[-1]):
            compact.append(line)
    return "\n".join(compact).strip()


def extract_vacancy(page, vacancy_id, link, debug=False):
    job = jobposting_data(page)
    title = get_text(page, ['h1[data-qa="vacancy-title"]', 'h1'])
    description = get_text(page, ['[data-qa="vacancy-description"]', '.vacancy-description'])
    if not title or not description:
        return None
    description = clean_description(description)

    area, structured_address = location_from_jobposting(job)
    # Селекторы HH периодически меняются. Если не нашли элемент, не заполняем поле по догадке.
    area = get_text(page, [
        '[data-qa="vacancy-view-location"]',
        '[data-qa="vacancy-view-area"]',
        '[data-qa="vacancy-location"]',
    ]) or area
    address = get_text(page, [
        '[data-qa="vacancy-view-raw-address"]',
        '[data-qa="vacancy-view-address"]',
        '[data-qa="vacancy-address"]',
    ]) or structured_address
    work_format, schedule = extract_work_details(page, description, job, debug=debug)

    return {
        'id': vacancy_id,
        'url': link,
        'title': title,
        'employer': get_text(page, ['[data-qa="vacancy-company-name"]', '[data-qa="company-name"]']) or '',
        'salary': get_text(page, ['[data-qa="vacancy-salary"]']) or '',
        'area': area,
        'address': address,
        'work_format': work_format,
        'schedule': schedule,
        'description': description,
        'collected_at': datetime.now(timezone.utc).isoformat(),
    }


def main():
    parser = argparse.ArgumentParser(description='Сбор вакансий HH через браузер')
    parser.add_argument('--query', default='системный аналитик', help='Поисковая фраза')
    parser.add_argument('--limit', type=int, default=10, help='Количество вакансий (1–20)')
    parser.add_argument('--debug-meta', action='store_true', help='Показать подсказки для диагностики формата и графика')
    parser.add_argument('--login', action='store_true', help='Открыть браузер для входа в HH, не собирать вакансии')
    args = parser.parse_args()
    if not 1 <= args.limit <= 20:
        parser.error('--limit должен быть от 1 до 20')

    search_url = 'https://hh.ru/search/vacancy?' + urlencode({'text': args.query, 'items_on_page': 20})
    vacancies = []
    with sync_playwright() as playwright:
        # launch_persistent_context сохраняет cookies, localStorage и вход между запусками.
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
            locale='ru-RU',
            viewport={'width': 1280, 'height': 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            if args.login:
                page.goto('https://hh.ru/', wait_until='domcontentloaded', timeout=45000)
                print('Войди в HH через открытый браузер. Когда закончишь, нажми Enter в терминале.')
                input()
                print('Профиль сохранён в папке:', PROFILE_DIR)
                return

            print(f'Открываю поиск: {search_url}')
            page.goto(search_url, wait_until='domcontentloaded', timeout=45000)
            page.wait_for_timeout(2000)
            if is_challenge(page):
                print('HH запросил проверку. Автоматический сбор остановлен.')
                return

            links = collect_links(page, args.limit)
            print(f'Найдено ссылок в выдаче: {len(links)}')
            if not links:
                print('Ссылок нет. Проверь открытое окно браузера и актуальность разметки HH.')
                return

            for index, (vacancy_id, link) in enumerate(links, 1):
                print(f'[{index}/{len(links)}] {link}')
                try:
                    page.goto(link, wait_until='domcontentloaded', timeout=45000)
                    page.wait_for_timeout(1000)
                    if is_challenge(page):
                        print('HH запросил проверку. Останавливаю сбор.')
                        break
                    vacancy = extract_vacancy(page, vacancy_id, link, debug=args.debug_meta)
                    if vacancy is None:
                        print('  Не удалось прочитать заголовок или описание; пропускаю.')
                        continue
                    vacancies.append(vacancy)
                    print('  Регион:', vacancy['area'] or 'не указан',
                          '| Формат:', vacancy['work_format'] or 'не указан')
                except Exception as error:
                    print(f'  Ошибка обработки: {error}')
        finally:
            if not args.login:
                OUTPUT.write_text(json.dumps(vacancies, ensure_ascii=False, indent=2), encoding='utf-8')
                print(f'Сохранено {len(vacancies)} вакансий: {OUTPUT}')
            context.close()


if __name__ == '__main__':
    main()