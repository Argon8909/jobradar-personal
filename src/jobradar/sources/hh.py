"""HH.ru adapter. Playwright only; CAPTCHA stops collection, never bypassed."""

import re
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlencode, urlparse

from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout, sync_playwright

from .base import RawVacancy, SourceError

_browser_lock = threading.Lock()
AREA_IDS = {"россия":"113","москва":"1","санкт-петербург":"2","казань":"88",
            "новосибирск":"4","екатеринбург":"3","нижний новгород":"66"}
FORMAT_IDS = {"remote":"REMOTE","hybrid":"HYBRID","office":"ON_SITE"}


def vacancy_links(page):
    links, seen = [], set()
    for href in page.locator('a[href*="/vacancy/"]').evaluate_all("els => els.map(el => el.href)"):
        parsed = urlparse(href)
        match = re.fullmatch(r"/vacancy/(\d+)/?",parsed.path)
        if match and (parsed.hostname == "hh.ru" or (parsed.hostname or "").endswith(".hh.ru")) and match[1] not in seen:
            seen.add(match[1]); links.append(match[1])
    return links


def challenge(page):
    return (any(word in page.url.lower() for word in ("captcha","challenge")) or
            page.locator('input[name="captcha"], [data-qa="captcha"], iframe[src*="captcha"]').count() > 0)


def text(page, selectors):
    for selector in selectors:
        locator = page.locator(selector).first
        if locator.count():
            value = locator.inner_text(timeout=3000).strip()
            if value:
                return value
    return None


def extract(page, external_id):
    title = text(page,['[data-qa="vacancy-title"]','h1'])
    description = text(page,['[data-qa="vacancy-description"]','.vacancy-description'])
    if not title or not description:
        raise SourceError("VACANCY_UNREADABLE","Не удалось прочитать полное объявление HH.ru.")
    employer = text(page,['[data-qa="vacancy-company-name"]','[data-qa="vacancy-company-name-link"]'])
    # Keep original header facts along with the full, unmodified description.
    metadata = []
    for selector in ('[data-qa="vacancy-salary"]','[data-qa="vacancy-view-raw-address"]',
                     '[data-qa="vacancy-view-location"]','[data-qa="vacancy-view-work-formats"]',
                     '[data-qa="vacancy-experience"]'):
        value = text(page,[selector])
        if value and value not in metadata:
            metadata.append(value)
    original = "\n\n".join([title,*([employer] if employer else []),*metadata,description])
    return RawVacancy("hh",str(external_id),f"https://hh.ru/vacancy/{external_id}",title,employer,original)


class HHAdapter:
    name = "hh"

    def __init__(self, settings):
        self.settings = settings
        self.profile_path = settings.database_path.parent / "browser-hh"
        self.areas = dict(AREA_IDS)

    @contextmanager
    def _browser(self, *, headless=None):
        if not _browser_lock.acquire(blocking=False):
            raise SourceError("SOURCE_BUSY","Браузер HH.ru уже используется другой задачей.")
        try:
            with sync_playwright() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(self.profile_path),headless=self.settings.values.get("hh",{}).get("headless",False) if headless is None else headless,
                    locale="ru-RU",viewport={"width":1280,"height":900})
                try:
                    yield context
                finally:
                    context.close()
        except BrowserTimeout:
            raise SourceError("SOURCE_TIMEOUT","Превышено время ожидания страницы HH.ru.",retryable=True) from None
        except BrowserError as error:
            if "ERR_CERT_" in str(error):
                raise SourceError("SOURCE_TLS","Chromium не доверяет TLS-сертификату HH.ru. Проверьте доверенные сертификаты системы.") from None
            raise SourceError("SOURCE_UNAVAILABLE","Не удалось открыть HH.ru. Проверьте установку Chromium и доступ к сайту.") from None
        finally:
            _browser_lock.release()

    def _goto(self, page, url, deadline=None):
        retries = self.settings.values["retries"]["max_safe_retries"]
        for attempt in range(retries + 1):
            try:
                return self._goto_once(page,url,deadline)
            except BrowserTimeout:
                if attempt == retries or (deadline is not None and time.monotonic() >= deadline):
                    raise

    def _goto_once(self, page, url, deadline=None):
        timeout = self.settings.values["source"]["page_timeout_seconds"] * 1000
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SourceError("TIMEOUT","Достигнут предел времени поиска.")
            timeout = min(timeout,max(1,int(remaining * 1000)))
        response = page.goto(url,wait_until="domcontentloaded",timeout=timeout)
        if challenge(page):
            raise SourceError("CAPTCHA","HH.ru запросил проверку пользователя. Сбор остановлен.")
        if response and response.status in (403,429):
            raise SourceError("SOURCE_BLOCKED","HH.ru временно ограничил доступ.")
        return response

    def _area(self, context, name, deadline):
        normalized = " ".join(name.split()).casefold()
        if normalized in self.areas:
            return self.areas[normalized]
        # Public reference data only; vacancy collection itself remains browser-based.
        try:
            response = context.request.get("https://api.hh.ru/areas",timeout=max(1,min(30000,int((deadline-time.monotonic())*1000))))
            if response.ok:
                queue = list(response.json())
                found = {}
                while queue:
                    item = queue.pop()
                    key = " ".join(item["name"].split()).casefold()
                    found.setdefault(key,[]).append(str(item["id"]))
                    queue.extend(item.get("areas",[]))
                self.areas.update({key:ids[0] for key,ids in found.items() if len(ids)==1})
        except (BrowserError, ValueError, KeyError, TypeError):
            pass
        if normalized not in self.areas:
            # No silent claim that an unrecognized hard filter was applied.
            raise SourceError("AREA_UNSUPPORTED",f"Не удалось определить географию HH.ru: {name}. Уточните название региона.")
        return self.areas[normalized]

    def search(self, query, *, limit, deadline):
        with self._browser() as context:
            areas = [self._area(context,name,deadline) for name in query.get("areas",[])]
            page = context.pages[0] if context.pages else context.new_page()
            detail_page = context.new_page()
            count, seen = 0, set()
            page_number = 0
            while count < limit and time.monotonic() < deadline:
                parameters = [("text",query["text"]),("items_on_page",20),("page",page_number)]
                parameters += [("area",area) for area in areas]
                parameters += [("work_format",FORMAT_IDS[value]) for value in query.get("filters",{}).get("work_formats",[])]
                self._goto(page,"https://hh.ru/search/vacancy?"+urlencode(parameters),deadline)
                try:
                    page.locator('a[href*="/vacancy/"]').first.wait_for(state="attached",timeout=min(5000,self.settings.values["source"]["page_timeout_seconds"]*1000))
                except BrowserTimeout:
                    if challenge(page):
                        raise SourceError("CAPTCHA","HH.ru запросил проверку пользователя.")
                    break
                links = [vid for vid in vacancy_links(page) if vid not in seen]
                if not links:
                    break
                for external_id in links:
                    if count >= limit or time.monotonic() >= deadline:
                        return
                    seen.add(external_id)
                    response = self._goto(detail_page,f"https://hh.ru/vacancy/{external_id}",deadline)
                    if response and response.status in (404,410):
                        continue
                    yield extract(detail_page,external_id)
                    count += 1
                page_number += 1

    def fetch_details(self, external_id):
        if not str(external_id).isdigit():
            raise SourceError("VALIDATION_ERROR","Некорректный ID вакансии HH.ru.")
        with self._browser() as context:
            page = context.new_page()
            response = self._goto(page,f"https://hh.ru/vacancy/{external_id}")
            if response and response.status in (404,410):
                raise SourceError("VACANCY_CLOSED","Объявление недоступно.")
            return extract(page,external_id)

    def check_status(self, external_id):
        try:
            self.fetch_details(external_id)
            return "available"
        except SourceError as exc:
            if exc.code == "VACANCY_CLOSED":
                return "closed"
            raise

    def login(self, wait=input):
        """Let the local user sign in/check the site manually in the same profile."""
        with self._browser(headless=False) as context:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto("https://hh.ru",wait_until="domcontentloaded",timeout=self.settings.values["source"]["page_timeout_seconds"]*1000)
            wait("Войдите в HH.ru в открытом браузере. После завершения нажмите Enter здесь: ")
