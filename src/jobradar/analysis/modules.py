"""Separate structured LLM contexts; no tools or SQL access."""

import json

from jsonschema import Draft202012Validator, ValidationError

from jobradar.llm.client import LLMError
from jobradar.storage import vacancies as repository
from .contracts import ASSESSMENT, CARD, EXTRACTION, PLANNER_OUTPUT, RESOLUTION


class StructuredLLM:
    def __init__(self, client, settings, budget):
        self.client, self.settings, self.budget = client, settings, budget

    def call(self, prompt_name, data, schema):
        self.budget.consume()
        prompt = (self.settings.prompts_dir / (prompt_name + ".md")).read_text(encoding="utf-8")
        response = self.client.complete([
            {"role":"system","content":prompt},
            {"role":"user","content":json.dumps({"input":data,"output_schema":schema},ensure_ascii=False,allow_nan=False)}
        ],json_mode=True,deadline=self.budget.deadline)
        try:
            result = json.loads(response["content"])
            json.dumps(result,allow_nan=False)
            Draft202012Validator(schema).validate(result)
        except (ValueError, KeyError, TypeError, ValidationError):
            raise LLMError("Модель вернула данные, не соответствующие схеме.") from None
        return result


class SearchPlanner:
    def __init__(self, structured):
        self.llm = structured

    def plan(self, state, request, sources):
        output = self.llm.call("search_planner",{
            "profile":state["profile"]["profile_data"],"criteria":state["context"]["criteria"],
            "request":request,"sources":list(sources),"supported_filters":["areas","work_formats"]},PLANNER_OUTPUT)
        plan = output["search_queries"]
        criteria = state["context"]["criteria"]
        for query in plan["queries"]:
            if query["source"] not in sources:
                raise LLMError("План поиска содержит неподключённый источник.")
            query.setdefault("filters",{})
            # The planner cannot silently discard the user's explicit hard filters.
            if criteria.get("locations"):
                query["areas"] = list(criteria["locations"])
            if criteria.get("work_formats"):
                query["filters"]["work_formats"] = list(criteria["work_formats"])
        plan["queries"].sort(key=lambda query:query.get("priority",0))
        return output


class VacancyAnalyzer:
    VERSION = 1

    def __init__(self, structured):
        self.llm = structured

    def extract(self, vacancy, state, request=None):
        result = self.llm.call("vacancy_analyzer",{
            "mode":"extract_and_assess","vacancy":vacancy,
            "profile":state["profile"]["profile_data"],"criteria":state["context"]["criteria"],"request":request},EXTRACTION)
        for tag in result["tags"]:
            if not tag["name"].strip() or not tag["evidence"].strip() or tag["evidence"] not in vacancy["description"]:
                raise LLMError("Подтверждение тега отсутствует в исходном объявлении.")
        card = result["card"]
        if card.get("salary_from") is not None and card.get("salary_to") is not None and card["salary_from"] > card["salary_to"]:
            raise LLMError("Границы зарплаты в карточке противоречат друг другу.")
        reserved = set(CARD["properties"]) | {"skills","tags","qualifications","licenses","certifications","requirements"}
        if any(item["key"] in reserved for item in card["structured_data"].get("additional_facts",[])):
            raise LLMError("Дополнительные факты дублируют нормализованные поля карточки.")
        return result

    def assess(self, card, tags, state, request=None):
        return self.llm.call("vacancy_analyzer",{
            "mode":"assessment_only","task":"Оцени сохранённую карточку; не извлекай её заново.",
            "card":card,"tags":tags,"profile":state["profile"]["profile_data"],
            "criteria":state["context"]["criteria"],"request":request},ASSESSMENT)


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
                result = self.llm.call("tag_resolver",{"name":tag["name"],"category":tag["category"],
                                      "evidence":tag["evidence"],"candidates":candidates},RESOLUTION)
                if result["decision"] == "ambiguous":
                    uncertainties.append("Неоднозначное соответствие тега: " + tag["name"])
                    continue
                if result["decision"] == "use_existing":
                    if result["tag_id"] not in {candidate["id"] for candidate in candidates}:
                        raise LLMError("Модель выбрала неизвестный тег.")
                    tag_id = result["tag_id"]
                elif result["tag_id"] is not None:
                    raise LLMError("Новый тег не должен содержать существующий идентификатор.")
            resolved.append({**tag,"tag_id":tag_id})
        return resolved, uncertainties
