"""Technical output schemas derived from the approved logical data model."""


def obj(properties, required=()):
    return {"type":"object", "properties":properties, "required":list(required), "additionalProperties":False}


TEXT = {"type":"string"}
NULL_TEXT = {"type":["string","null"]}
NUMBER = {"type":["number","null"]}
NONNEGATIVE = {**NUMBER,"minimum":0}
STRINGS = {"type":"array", "items":TEXT}
SCALAR = {"type":["string","number","boolean","null"]}
STRUCTURED = obj({
    "version":{"const":1}, "responsibilities":STRINGS,
    "schedule":{"anyOf":[{"type":"null"},obj({"type":NULL_TEXT,"pattern":NULL_TEXT,"hours_per_shift":NUMBER})]},
    "travel":{"anyOf":[{"type":"null"},obj({"required":{"type":["boolean","null"]},"frequency":NULL_TEXT})]},
    "employment":{"anyOf":[{"type":"null"},obj({"contract_type":{"enum":["employment","contractor","self_employed","other","unknown",None]},"duration":NULL_TEXT})]},
    "conditions":{"type":"array","items":obj({"name":TEXT,"value":TEXT},("name","value"))},
    "additional_facts":{"type":"array","items":obj({"key":TEXT,"value":SCALAR,"description":TEXT},("key","value","description"))},
    "uncertainties":STRINGS,
},("version",))
CARD = obj({
    "profession":NULL_TEXT,"specialization":NULL_TEXT,"area":NULL_TEXT,
    "work_format":{"enum":["remote","hybrid","office","unknown",None]},
    "salary_from":NONNEGATIVE,"salary_to":NONNEGATIVE,"salary_currency":NULL_TEXT,
    "experience_min":NONNEGATIVE,"summary":NULL_TEXT,"structured_data":STRUCTURED,
},("structured_data",))
ASSESSMENT = obj({
    "relevance_score":{**NUMBER,"description":"Необязательная численная оценка, выше — релевантнее; null при отсутствии достоверного значения."},
    "decision":{**TEXT,"description":"Краткий вывод о соответствии, включая неизвестные сведения."},
    "reasoning":{**TEXT,"minLength":1},
},("decision","reasoning"))
TAG = obj({"category":{**TEXT,"pattern":"^[a-z][a-z0-9_]*$"},"name":{**TEXT,"minLength":1},
           "requirement_level":{"enum":["required","optional","mentioned"]},
           "evidence":{**TEXT,"minLength":1}},("category","name","requirement_level","evidence"))
EXTRACTION = obj({"card":CARD,"tags":{"type":"array","maxItems":40,"items":TAG},"assessment":ASSESSMENT},("card","tags","assessment"))
RESOLUTION = obj({"decision":{"enum":["use_existing","create_new","ambiguous"]},
                  "tag_id":{"type":["integer","null"]}},("decision","tag_id"))
HH_FILTERS = obj({"work_formats":{"type":"array","items":{"enum":["remote","hybrid","office"]}}})
QUERY = obj({"source":TEXT,"text":{**TEXT,"minLength":1},"areas":STRINGS,"filters":HH_FILTERS,
             "priority":{"type":"integer"}},("source","text"))
PLAN = obj({"version":{"const":1},"queries":{"type":"array","minItems":1,"maxItems":8,"items":QUERY}},("version","queries"))
PUBLICATION_PERIOD = obj({"unit":{"enum":["days","weeks","months","default","unlimited"]},
                          "value":{"type":["integer","null"],"minimum":1},
                          "evidence":{"type":"string","minLength":1}},("unit","value","evidence"))
GEOGRAPHY_CHANGE = obj({"locations":{**STRINGS,"maxItems":30},
                        "evidence":{**TEXT,"minLength":1}},("locations","evidence"))
PLANNER_OUTPUT = obj({"geography_change":GEOGRAPHY_CHANGE,"publication_period":PUBLICATION_PERIOD,"search_queries":PLAN,"resolved_locations":{"type":"array","maxItems":30,"items":obj({"input":TEXT,"areas":{"type":"array","minItems":1,"maxItems":30,"items":TEXT},"explanation":TEXT},("input","areas","explanation"))},"target_count":{"type":["integer","null"],"minimum":1,"maximum":2147483647,
                     "description":"Явно запрошенное число вакансий для изучения; null если пользователь не указал."}},("search_queries","target_count"))
