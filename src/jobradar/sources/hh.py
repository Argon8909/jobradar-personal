"""HH.ru adapter. Browser-only collection and bounded manual challenge recovery."""

import json
import re
import threading
import time
from contextlib import contextmanager
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlparse

from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout, sync_playwright

from .base import RawVacancy, SourceError, SourceEvent

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


def authentication_required(page):
    return urlparse(page.url).path.startswith(("/account/login","/applicant/login"))


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts = []; self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script","style"):
            self.hidden += 1
        if tag in ("br","p","div","li"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script","style") and self.hidden:
            self.hidden -= 1
        if tag in ("p","div","li"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def job_posting(page):
    for content in page.locator('script[type="application/ld+json"]').all_text_contents():
        try:
            queue = [json.loads(content)]
        except (ValueError,TypeError):
            continue
        while queue:
            item = queue.pop()
            if isinstance(item,list):
                queue.extend(item)
            elif isinstance(item,dict):
                types = item.get("@type",[])
                if types == "JobPosting" or isinstance(types,list) and "JobPosting" in types:
                    return item
                graph = item.get("@graph")
                if isinstance(graph,list):
                    queue.extend(graph)
    return {}


def text(page, selectors):
    for selector in selectors:
        locator = page.locator(selector).first
        if locator.count():
            value = locator.inner_text(timeout=3000).strip()
            if value:
                return value
    return None


def extract(page, external_id):
    fallback = job_posting(page)
    title = text(page,['[data-qa="vacancy-title"]','h1'])
    description = text(page,['[data-qa="vacancy-description"]','.vacancy-description'])
    title = title or fallback.get("title") or fallback.get("name")
    if not description and isinstance(fallback.get("description"),str):
        parser = PlainText(); parser.feed(fallback["description"])
        description = "".join(parser.parts).strip()
    if not isinstance(title,str) or not title.strip() or not isinstance(description,str) or not description.strip():
        raise SourceError("VACANCY_UNREADABLE","Не удалось прочитать полное объявление HH.ru.")
    employer = text(page,['[data-qa="vacancy-company-name"]','[data-qa="vacancy-company-name-link"]'])
    if not employer and isinstance(fallback.get("hiringOrganization"),dict):
        employer = fallback["hiringOrganization"].get("name")
    if not isinstance(employer,str):
        employer = None
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
            raise SourceError("CAPTCHA","HH.ru запросил проверку пользователя.")
        if authentication_required(page):
            raise SourceError("AUTH_REQUIRED","HH.ru запросил вход в учётную запись.")
        if response and response.status in (403,429):
            raise SourceError("SOURCE_BLOCKED","HH.ru временно ограничил доступ.")
        return response

    def _navigate(self, page, url, deadline):
        try:
            return self._goto(page,url,deadline)
        except SourceError as exc:
            if exc.code not in ("CAPTCHA","AUTH_REQUIRED"):
                raise
            code, message = exc.code, str(exc)
        if self.settings.values.get("hh",{}).get("headless",False):
            raise SourceError(code,message + " Откройте видимый браузер командой hh-login и повторите поиск.")
        page.bring_to_front()
        seconds = self.settings.values.get("hh",{}).get("manual_action_timeout_seconds",120)
        until = min(deadline,time.monotonic()+seconds)
        yield SourceEvent("source_action_required",code,message + " Завершите проверку или вход в открытом окне HH.ru; поиск продолжится автоматически.",stage="source_navigation")
        while time.monotonic() < until:
            if not challenge(page) and not authentication_required(page):
                response = self._goto(page,url,deadline)
                yield SourceEvent("source_resumed","SOURCE_RESUMED","Проверка завершена, продолжаем поиск.",stage="source_navigation")
                return response
            page.wait_for_timeout(min(500,max(1,int((until-time.monotonic())*1000))))
        raise SourceError(code + "_WAIT_EXPIRED",message + " Время ожидания ручного действия истекло; собранные результаты сохранены.")

    def _content_timeout(self, deadline):
        return max(1,min(self.settings.values["source"].get("content_timeout_seconds",15)*1000,int((deadline-time.monotonic())*1000)))

    def _wait_content(self, page, deadline):
        try:
            page.wait_for_function("""() => {
              const el = document.querySelector('[data-qa="vacancy-description"], .vacancy-description');
              if (el && el.innerText.trim()) return true;
              if (document.querySelector('input[name="captcha"], [data-qa="captcha"], iframe[src*="captcha"]') || /captcha|challenge/.test(location.href)) return true;
              return [...document.querySelectorAll('script[type="application/ld+json"]')].some(el => {
                try { const queue = [JSON.parse(el.textContent)]; while (queue.length) {
                  const item = queue.pop(); if (Array.isArray(item)) queue.push(...item);
                  else if (item && typeof item === 'object') {
                    if ((item['@type'] === 'JobPosting' || (Array.isArray(item['@type']) && item['@type'].includes('JobPosting'))) && item.description) return true;
                    if (Array.isArray(item['@graph'])) queue.push(...item['@graph']);
                  }
                }} catch (_) {} return false;
              });
            }""",timeout=self._content_timeout(deadline))
        except BrowserTimeout:
            pass  # extract supplies an explicit per-vacancy failure.
        if challenge(page):
            raise SourceError("CAPTCHA","HH.ru запросил проверку пользователя.")

    def _area(self, context, name, deadline):
        normalized = " ".join(name.split()).casefold()
        if normalized in self.areas:
            return self.areas[normalized]
        page = context.new_page()
        # Resolve unknown regions through the visible site controls only.
        try:
            yield from self._navigate(page,"https://hh.ru/search/vacancy/advanced",deadline)
            control = page.locator('[data-qa="advanced-search-region-selectFromList"], [data-qa="serp__novafilter-regions"]').first
            control.click(timeout=self._content_timeout(deadline))
            dialog = page.get_by_role("dialog")
            field = dialog.get_by_role("textbox").first
            if field.count():
                field.fill(name,timeout=self._content_timeout(deadline))
            choice = dialog.get_by_role("checkbox",name=name,exact=True)
            choice.first.wait_for(state="attached",timeout=self._content_timeout(deadline))
            if choice.count() == 1:
                value = choice.get_attribute("value")
                choice.check(timeout=self._content_timeout(deadline))
                if value and value.isdecimal():
                    self.areas[normalized] = value
                else:
                    dialog.get_by_role("button",name=re.compile("Выбрать|Применить|Готово")).first.click(timeout=self._content_timeout(deadline))
                    page.locator('[data-qa="advanced-search-submit-button"]').click(timeout=self._content_timeout(deadline))
                    page.wait_for_url("**/search/vacancy?**",timeout=self._content_timeout(deadline))
                    ids = parse_qs(urlparse(page.url).query).get("area",[])
                    if len(ids) == 1 and ids[0].isdecimal():
                        self.areas[normalized] = ids[0]
        except BrowserTimeout:
            pass
        finally:
            page.close()
        if normalized not in self.areas:
            # No silent claim that an unrecognized hard filter was applied.
            raise SourceError("AREA_UNSUPPORTED",f"Не удалось определить географию HH.ru: {name}. Уточните название региона.")
        return self.areas[normalized]

    def search(self, query, *, limit, deadline):
        with self._browser() as context:
            areas = []
            for name in query.get("areas",[]):
                areas.append((yield from self._area(context,name,deadline)))
            page = context.pages[0] if context.pages else context.new_page()
            detail_page = context.new_page()
            count, seen = 0, set()
            page_number = 0
            while count < limit and time.monotonic() < deadline:
                parameters = [("text",query["text"]),("items_on_page",20),("page",page_number)]
                parameters += [("area",area) for area in areas]
                parameters += [("work_format",FORMAT_IDS[value]) for value in query.get("filters",{}).get("work_formats",[])]
                search_url = "https://hh.ru/search/vacancy?"+urlencode(parameters)
                yield from self._navigate(page,search_url,deadline)
                for attempt in range(2):
                    try:
                        page.wait_for_function("""() => !!document.querySelector('a[href*="/vacancy/"], [data-qa="vacancy-serp__no-vacancies"], [data-qa="vacancy-serp__vacancy_not_found"], input[name="captcha"], [data-qa="captcha"], iframe[src*="captcha"]') || /captcha|challenge/.test(location.href)""",timeout=self._content_timeout(deadline))
                    except BrowserTimeout:
                        raise SourceError("SEARCH_LAYOUT_UNREADABLE","Не удалось прочитать выдачу HH.ru; пустая выдача не подтверждена.") from None
                    if challenge(page) and attempt == 0:
                        yield from self._navigate(page,search_url,deadline)
                    else:
                        break
                if challenge(page):
                    raise SourceError("CAPTCHA","HH.ru повторно запросил проверку; сбор остановлен.")
                candidates = vacancy_links(page)
                if not candidates and not page.locator('[data-qa="vacancy-serp__no-vacancies"], [data-qa="vacancy-serp__vacancy_not_found"]').count():
                    raise SourceError("SEARCH_LAYOUT_UNREADABLE","Не удалось подтвердить список вакансий или пустую выдачу HH.ru.")
                links = [vid for vid in candidates if vid not in seen]
                if not links:
                    break
                for external_id in links:
                    if count >= limit or time.monotonic() >= deadline:
                        return
                    seen.add(external_id)
                    count += 1
                    url = f"https://hh.ru/vacancy/{external_id}"
                    try:
                        response = yield from self._navigate(detail_page,url,deadline)
                        if response and response.status in (404,410):
                            raise SourceError("VACANCY_CLOSED","Объявление недоступно или удалено.")
                        try:
                            self._wait_content(detail_page,deadline)
                        except SourceError as exc:
                            if exc.code != "CAPTCHA":
                                raise
                            yield from self._navigate(detail_page,url,deadline)
                            self._wait_content(detail_page,deadline)
                        yield extract(detail_page,external_id)
                    except BrowserTimeout:
                        yield SourceEvent("source_warning","VACANCY_TIMEOUT","Превышено время чтения объявления; оно пропущено.",external_id,url,"source_extraction")
                    except SourceError as exc:
                        if exc.code not in ("VACANCY_CLOSED","VACANCY_UNREADABLE"):
                            raise
                        yield SourceEvent("source_warning",exc.code,str(exc),external_id,url,"source_extraction")
                page_number += 1

    def fetch_details(self, external_id):
        if not str(external_id).isdigit():
            raise SourceError("VALIDATION_ERROR","Некорректный ID вакансии HH.ru.")
        with self._browser() as context:
            page = context.new_page()
            response = self._goto(page,f"https://hh.ru/vacancy/{external_id}")
            if response and response.status in (404,410):
                raise SourceError("VACANCY_CLOSED","Объявление недоступно.")
            self._wait_content(page,time.monotonic()+self.settings.values["source"]["page_timeout_seconds"])
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
