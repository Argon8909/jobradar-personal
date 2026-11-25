"""Separate structured LLM contexts; no tools or SQL access."""

import copy
import json
import re
import time

from jsonschema import Draft202012Validator

from jobradar.llm.client import LLMError
from jobradar.storage import vacancies as repository
from .contracts import ASSESSMENT, CARD, EXTRACTION, PLANNER_OUTPUT, RESOLUTION, TAG
from .errors import AnalysisError
from jobradar.search.budget import SearchStop
from jobradar.search.period import assessment_criteria, validate_period


class StructuredLLM:
    def __init__(self, client, settings, budget, observer=None, recorder=None):
        self.client, self.settings, self.budget = client, settings, budget
        self.observer = observer
        self.recorder = recorder

    def _record(self, prompt_name, messages, schema, response, attempt, start, error=None):
        if self.recorder:
            self.recorder.llm_call(prompt_name=prompt_name,messages=messages,schema=schema,
                model=self.settings.values["llm"]["model"],response=response,attempt=attempt,
                elapsed_ms=round((time.monotonic()-start)*1000),error=error.diagnostic() if error else None)

    def warn(self, code, stage, message, *, path=None, details=None):
        if self.observer:
            self.observer(AnalysisError(code,stage,message,path=path,details=details).diagnostic("warning"))

    def note(self, code, stage, message, *, details):
        if self.observer:
            self.observer(AnalysisError(code,stage,message,details=details).diagnostic("info"))

    def call(self, prompt_name, data, schema, *, validation_schema=None, check=None):
        prompt = (self.settings.prompts_dir / (prompt_name + ".md")).read_text(encoding="utf-8")
        messages = [
            {"role":"system","content":prompt},
            {"role":"user","content":json.dumps({"input":data,"output_schema":schema},ensure_ascii=False,allow_nan=False)}
        ]
        stage = data.get("mode",prompt_name)
        repairs = self.settings.values["retries"].get("max_structural_repairs",1)
        for attempt in range(repairs + 1):
            self.budget.consume()
            if attempt and self.observer:
                self.observer({**error.diagnostic("warning"),"code":"REPAIR_REQUESTED","validation_code":error.code,
                               "message":"Запрошено однократное исправление структуры ответа модели."})
            try:
                start = time.monotonic()
                calls_before = self.budget.calls
                response = self.client.complete(messages,json_mode=True,deadline=self.budget.deadline,on_retry=self.budget.consume)
            except SearchStop as exc:
                error = AnalysisError(exc.code,stage,str(exc))
                self._record(prompt_name,messages,schema,{"content":None,"transport_attempts":1+self.budget.calls-calls_before},attempt,start,error)
                raise
            except LLMError as exc:
                error = AnalysisError(exc.code,stage,str(exc))
                self._record(prompt_name,messages,schema,{"content":getattr(exc,"response_content",None),
                    "transport_attempts":1+self.budget.calls-calls_before},attempt,start,error)
                raise error from None
            content = response.get("content")
            try:
                result = json.loads(content)
                json.dumps(result,allow_nan=False)
            except (ValueError,TypeError) as exc:
                details = {key:getattr(exc,key) for key in ("lineno","colno","pos","msg") if hasattr(exc,key)}
                error = AnalysisError("LLM_JSON_INVALID",stage,"Ответ модели не является корректным JSON.",path="/",rule="json",details=details)
            else:
                invalid = next(Draft202012Validator(validation_schema or schema).iter_errors(result),None)
                if invalid:
                    # Validator messages can contain private source/model data.
                    path = "/" + "/".join(str(part).replace("~","~0").replace("/","~1") for part in invalid.absolute_path)
                    error = AnalysisError("LLM_SCHEMA_INVALID",stage,"Ответ модели нарушает схему данных.",path=path,rule=invalid.validator,
                        details={"explanation":str(invalid.message)[:3000],"expected":invalid.validator_value})
                else:
                    try:
                        if check:
                            check(result)
                        self._record(prompt_name,messages,schema,response,attempt,start)
                        return result
                    except AnalysisError as exc:
                        error = exc
            self._record(prompt_name,messages,schema,response,attempt,start,error)
            if attempt == repairs:
                raise error
            messages += [{"role":"assistant","content":content if isinstance(content,str) else ""},
                         {"role":"user","content":json.dumps({"task":"Исправь ответ по исходным данным и output_schema; не добавляй неизвестных фактов.",
                            "validation_error":{"code":error.code,"path":error.path,"rule":error.rule},"output_schema":schema},ensure_ascii=False)}]


class SearchPlanner:
    def __init__(self, structured):
        self.llm = structured

    def plan(self, state, request, sources):
        output = self.llm.call("search_planner",{
            "profile":state["profile"]["profile_data"],"criteria":state["context"]["criteria"],
            "request":request,"sources":list(sources),"supported_filters":["areas","work_formats"]},PLANNER_OUTPUT)
        override = output.get("publication_period")
        if override is not None:
            try:
                if not request or override["evidence"] not in request:
                    raise ValueError("Период не подтверждён текущим запросом.")
                validate_period({key:override[key] for key in ("unit","value")})
            except ValueError as error:
                raise SearchStop("PERIOD_CLARIFICATION",str(error)) from None
        plan = output["search_queries"]
        criteria = state["context"]["criteria"]
        self.llm.note("LOCATION_PLAN","search_planner","География активного контекста и предложение модели.",details={
            "context_id":state["active_context_id"],"saved_locations":criteria.get("locations",[]),
            "proposed_queries":[{"text":query["text"],"areas":query.get("areas",[])} for query in plan["queries"]],
            "geography_change":output.get("geography_change")})
        remote = {"удалённо","удаленно","удалёнка","удаленка","remote","удалённая работа","удаленная работа"}
        normalize = lambda names: {" ".join(name.split()).casefold() for name in names
                                   if name.strip().casefold() not in remote}
        change = output.get("geography_change")
        if change:
            if not request or not change["evidence"].strip() or change["evidence"] not in request:
                raise SearchStop("LOCATION_CLARIFICATION","Не удалось подтвердить изменение географии текущим запросом. Уточните нужные регионы; условия не изменены.")
            if normalize(change["locations"]) != normalize(criteria.get("locations",[])):
                raise SearchStop("LOCATION_CLARIFICATION",
                    "Вы запросили географию, отличную от сохранённой географии активного контекста. "
                    "Разовая смена пока не поддерживается; нужно ли сохранить новые регионы в этом контексте? "
                    "Без вашего разрешения условия не изменены, поиск не выполнен.")
        resolutions = {item["input"].strip().casefold():item for item in output.get("resolved_locations",[])}
        broad = re.compile(r"ближн\w*\s+зарубеж\w*|ближайш\w*\s+зарубеж\w*|постсовет\w*|\bснг\b|\bевроп\w*\b|\bзарубеж\w*\b",re.I)
        for query in plan["queries"]:
            proposed_areas = query.get("areas",[])
            if broad.search(query["text"]):
                raise SearchStop("LOCATION_CLARIFICATION","Уточните страны поиска: географическую группу нужно преобразовать в регионы, а не использовать как ключевые слова вакансии.")
            if query["source"] not in sources:
                raise LLMError("План поиска содержит неподключённый источник.")
            query.setdefault("filters",{})
            # The planner cannot silently discard the user's explicit hard filters.
            locations = criteria.get("locations",[])
            resolved = []
            for name in locations:
                if broad.search(name):
                    item = resolutions.get(name.strip().casefold())
                    if not item or any(broad.search(area) for area in item["areas"]):
                        raise SearchStop("LOCATION_CLARIFICATION","Какие страны включить в поиск за рубежом? Например, Беларусь, Казахстан, Армению или другие — укажите подходящие вам.")
                    resolved.extend(item["areas"])
                    self.llm.warn("LOCATION_RESOLVED","search_planner",f"География «{name}»: {', '.join(item['areas'])}. {item['explanation']}")
                else:
                    resolved.append(name)
            locations = list(dict.fromkeys(resolved))
            query["areas"] = [name for name in locations if name.strip().casefold() not in remote]
            if normalize(proposed_areas) != normalize(query["areas"]):
                self.llm.note("LOCATION_PLAN_CORRECTED","search_planner",
                    "Регионы предложения модели заменены действующей географией активного контекста.",
                    details={"proposed_areas":proposed_areas,"applied_areas":query["areas"]})
            if len(query["areas"]) != len(locations):
                self.llm.warn("LOCATION_IS_FORMAT","search_planner","Удалённая работа распознана как формат работы, а не география.")
            # Algorithm 1.0 selects only keywords and areas by default. The
            # adapter retains work_formats support for explicit future policies.
            # Model-proposed source options never override this server policy.
            if any(name.strip().isdecimal() for name in query["areas"]):
                raise SearchStop("LOCATION_CLARIFICATION","Укажите названия стран или городов, а не числовые HH-ID.")
            query["filters"] = {}
        plan["queries"].sort(key=lambda query:query.get("priority",0))
        return output


class VacancyAnalyzer:
    VERSION = 1

    def __init__(self, structured):
        self.llm = structured

    def extract(self, vacancy, state, request=None):
        envelope = copy.deepcopy(EXTRACTION)
        # Tag validation is independent of the usable core card/assessment.
        envelope["properties"]["tags"]["items"] = {}
        result = self.llm.call("vacancy_analyzer",{
            "mode":"extract_and_assess","vacancy":vacancy,
            "profile":state["profile"]["profile_data"],"criteria":assessment_criteria(state["context"]["criteria"]),"request":request},EXTRACTION,
            validation_schema=envelope,check=self._check_card)
        valid = []
        for index,tag in enumerate(result["tags"]):
            invalid = not Draft202012Validator(TAG).is_valid(tag)
            match = None
            if not invalid and tag["name"].strip() and tag["evidence"].strip():
                match = re.search(r"\s+".join(re.escape(word) for word in tag["evidence"].split()),vacancy["description"])
            if not match:
                self.llm.warn("TAG_INVALID" if invalid else "TAG_EVIDENCE_MISSING","tag_validation",
                              "Тег пропущен: неверная структура или нет подтверждённой цитаты в объявлении.",path=f"/tags/{index}",details={"tag":tag})
                result["card"]["structured_data"].setdefault("uncertainties",[]).append("Один из тегов не прошёл проверку; карточка и оценка сохранены без него.")
                continue
            valid.append({**tag,"evidence":match.group(0)})
        result["tags"] = valid
        return result

    @staticmethod
    def _check_card(result):
        card = result["card"]
        if card.get("salary_from") is not None and card.get("salary_to") is not None and card["salary_from"] > card["salary_to"]:
            raise AnalysisError("CARD_SALARY_INVALID","extract_and_assess","Границы зарплаты в карточке противоречат друг другу.",path="/card/salary_from",rule="salary_bounds")
        reserved = set(CARD["properties"]) | {"skills","tags","qualifications","licenses","certifications","requirements"}
        if any(item["key"] in reserved for item in card["structured_data"].get("additional_facts",[])):
            raise AnalysisError("CARD_FACT_DUPLICATE","extract_and_assess","Дополнительные факты дублируют нормализованные поля карточки.",path="/card/structured_data/additional_facts",rule="normalized_fields")

    def assess(self, card, tags, state, request=None):
        return self.llm.call("vacancy_analyzer",{
            "mode":"assessment_only","task":"Оцени сохранённую карточку; не извлекай её заново.",
            "card":card,"tags":tags,"profile":state["profile"]["profile_data"],
            "criteria":assessment_criteria(state["context"]["criteria"]),"request":request},ASSESSMENT)


class TagResolver:
    def __init__(self, database, structured):
        self.database, self.llm = database, structured

    def resolve(self, tags):
        resolved, uncertainties = [], []
        for tag in tags:
            with self.database.connection() as db:
                exact = repository.find_tag(db,tag["category"],tag["name"])
                candidates = repository.tag_candidates(db,tag["category"],tag["name"])
            tag_id = exact["id"] if exact else None
            if not exact and candidates:
                try:
                    result = self.llm.call("tag_resolver",{"name":tag["name"],"category":tag["category"],
                                          "evidence":tag["evidence"],"candidates":candidates},RESOLUTION)
                except (LLMError,SearchStop) as exc:
                    self.llm.warn(exc.code,"tag_resolution","Не удалось сопоставить тег; корректные карточка и оценка сохранены без него.")
                    uncertainties.append("Сопоставление одного из тегов не завершено.")
                    continue
                if result["decision"] == "ambiguous":
                    self.llm.warn("TAG_AMBIGUOUS","tag_resolution","Тег не сопоставлен однозначно.",details={"tag":tag,"resolution":result})
                    uncertainties.append("Неоднозначное соответствие тега: " + tag["name"])
                    continue
                if result["decision"] == "use_existing":
                    if result["tag_id"] not in {candidate["id"] for candidate in candidates}:
                        self.llm.warn("TAG_ID_INVALID","tag_resolution","Тег пропущен: модель выбрала неизвестный идентификатор.")
                        uncertainties.append("Модель выбрала неизвестный идентификатор тега.")
                        continue
                    tag_id = result["tag_id"]
                elif result["tag_id"] is not None:
                    self.llm.warn("TAG_ID_INVALID","tag_resolution","Тег пропущен: противоречивое решение о создании тега.")
                    uncertainties.append("Противоречивое решение о создании одного из тегов.")
                    continue
            resolved.append({**tag,"tag_id":tag_id})
        return resolved, uncertainties
