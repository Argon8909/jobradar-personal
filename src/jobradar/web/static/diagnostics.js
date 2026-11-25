"use strict";
const el = id => document.getElementById(id);
let report = null, loading = false, timer = null;
async function api(url) {
  const response = await fetch(url,{credentials:"same-origin"});
  const body = await response.json();
  if (!response.ok) throw Error(body.error?.message || "Не удалось загрузить диагностику.");
  return body;
}
function paragraph(parent,text) { const p=document.createElement("p");p.textContent=text;parent.append(p);return p; }
function json(parent,title,value) {
  const details=document.createElement("details"),summary=document.createElement("summary"),pre=document.createElement("pre");
  summary.textContent=title;pre.textContent=typeof value==="string" ? value : JSON.stringify(value,null,2);
  details.dataset.key=(parent.dataset.key || "")+title;
  details.append(summary,pre);parent.append(details);
}
function issuesView() {
  const parent=el("issues");parent.replaceChildren();
  const filter=el("filter").value.toLowerCase(),level=el("severity").value;
  const issues=(report?.diagnostics?.issues || []).filter(i => (!level || i.severity===level) && (!filter || JSON.stringify(i).toLowerCase().includes(filter)));
  for (const item of issues) {
    const row=document.createElement("div");row.className=item.severity || "info";
    paragraph(row,`${item.time || ""} ${item.severity}: ${item.code} · ${item.stage || ""}${item.vacancy_id ? ` · вакансия #${item.vacancy_id}` : ""}`);
    paragraph(row,item.message);json(row,"Все поля события",item);parent.append(row);
  }
  if (!issues.length) paragraph(parent,"Нет событий по выбранному фильтру.");
}
function render() {
  const opened=new Set([...document.querySelectorAll("details[open]")].map(node => node.dataset.key));
  el("summary").replaceChildren();el("calls").replaceChildren();el("unassessed").replaceChildren();
  el("export").disabled=!report;
  if (!report) {el("plan").textContent="";el("issues").replaceChildren();el("status").textContent="В активном контексте нет поисков.";return;}
  const c=report.counts;
  paragraph(el("summary"),`Поиск #${report.task_id} · ${report.status}${report.is_partial ? " · частично" : ""} · найдено ${c.found_count} · оценено ${c.evaluated_count} · без оценки ${report.unassessed_count}`);
  paragraph(el("summary"),`Создан: ${report.created_at || "—"}; завершён: ${report.completed_at || "—"}`);
  if (report.stop_reason) paragraph(el("summary"),`Причина завершения: ${report.stop_reason}`);
  const trace=report.diagnostics;
  const period=report.publication_period || trace?.publication_period;
  if (period) {
    const units={days:"календарных дней",weeks:"недель",months:"календарных месяцев"};
    const sources={request:"текущий запрос",context:"поисковый контекст",configuration:"конфигурация"};
    paragraph(el("summary"),`Период: ${period.unit==="unlimited" ? "без ограничения по дате" : `${period.value} ${units[period.unit]}`} · источник: ${sources[period.source]}${period.reset_to_default ? " (возврат к стандарту)" : ""}`);
    paragraph(el("summary"),`Начало: ${period.started_at}; включительная граница: ${period.cutoff || "нет"}; календарь: ${period.timezone}.`);
  }
  if (trace) {
    const labels={specialized_llm_calls:"Попыток специализированных LLM-вызовов (с сетевыми повторами)",analysis_attempts:"Попыток нового анализа",analysis_succeeded:"Успешно",analysis_failed:"Ошибок",cache_hits:"Из кэша",reassessments:"Переоценок",repair_requests:"Исправлений структуры",tag_warnings:"Предупреждений тегов",source_skipped:"Не прочитано на сайте",duplicate_skipped:"Повторной обработки пропущено",hh_discovered:"Уникальных обнаружений HH",hh_duplicates:"Повторов HH",hh_read:"Полных исходников HH",hh_saved:"Сохранений HH",hh_incomplete:"Неполных исходников HH",local_candidates:"Локальных кандидатов",filtered:"Отфильтровано",deferred:"Отложено",visible_results:"Результатов для выдачи",age_excluded:"Исключено по возрасту",unknown_publication_date:"Дата публикации неизвестна",availability_unconfirmed:"Доступность не подтверждена в этом поиске"};
    for (const [key,value] of Object.entries(trace.metrics)) {const p=paragraph(el("summary"),`${labels[key] || key}: ${value}`);p.className="metric";}
    if (trace.payload_limit_reached) paragraph(el("summary"),"Достигнут размер файла: часть тел ответов удалена, метаданные сохранены.");
    if (trace.coverage) paragraph(el("summary"),`Запросов завершено: ${trace.coverage.completed_queries}/${trace.coverage.planned_queries}. Охват источника: ${trace.coverage.source_complete ? "завершены запланированные запросы" : "неполный"}.`);
  } else paragraph(el("summary"),"Подробный журнал отсутствует: старый поиск, отключённая запись, очистка по сроку или ошибка сохранения.");
  el("plan").textContent=JSON.stringify(report.search_plan || trace?.search_plan || null,null,2);
  issuesView();
  for (const call of trace?.llm_calls || []) {
    const box=document.createElement("details"),title=document.createElement("summary");
    box.dataset.key=`task-${report.task_id}-call-${call.id}`;
    title.textContent=`#${call.id} ${call.prompt} · ${call.model} · ${call.status} · ${call.elapsed_ms} мс · попытка ${call.attempt}${call.vacancy_id ? ` · вакансия #${call.vacancy_id}` : ""}`;
    box.append(title);
    json(box,"Метаданные и ошибка",Object.fromEntries(Object.entries(call).filter(([key]) => !["response","request","schema","system_prompt"].includes(key))));
    for (const [key,label] of [["response","Исходный ответ модели"],["request","Исходные данные запроса"],["schema","Схема ответа"],["system_prompt","Системный промпт"]]) {
      if (Object.hasOwn(call,key)) json(box,label,call[key]);
    }
    if (!Object.hasOwn(call,"response")) paragraph(box,"Тело ответа не сохранено согласно настройкам или удалено по лимиту размера.");
    if (call.response_truncated) paragraph(box,"Ответ в журнале усечён по лимиту длины.");
    el("calls").append(box);
  }
  if (!(trace?.llm_calls || []).length) paragraph(el("calls"),"Записанных вызовов LLM нет.");
  for (const item of report.unassessed) {
    const p=paragraph(el("unassessed"),`#${item.vacancy_id} ${item.title} — ${item.reason} ${item.error_code || ""} ${item.path || ""}`);
    if (/^https?:\/\//i.test(item.url || "")) {const a=document.createElement("a");a.href=item.url;a.textContent="Открыть вакансию";a.target="_blank";a.rel="noopener noreferrer";p.append(document.createTextNode(" "),a);}
  }
  el("status").textContent=`Обновлено: ${new Date().toLocaleTimeString()}`;
  document.querySelectorAll("details").forEach(node => {node.open=opened.has(node.dataset.key);});
}
async function load(keep=true) {
  if (loading) return;loading=true;clearTimeout(timer);
  try {
    const before=keep ? el("tasks").value : "";
    const list=await api("/api/search-diagnostics/tasks");
    el("context").textContent=list.context_name || "Общий диалог — выберите контекст в чате";
    const latest=document.createElement("option");latest.value="";latest.textContent="Последний поиск (автоматически)";
    el("tasks").replaceChildren(latest,...list.tasks.map(task => {const option=document.createElement("option");option.value=task.id;option.textContent=`#${task.id} · ${task.state} · ${task.created_at}`;return option;}));
    if (list.tasks.some(task => String(task.id)===before)) el("tasks").value=before;
    report=list.tasks.length ? await api(`/api/search-diagnostics${el("tasks").value ? `?task_id=${el("tasks").value}` : ""}`) : null;
    render();el("error").textContent="";
  } catch (error) {
    report=null;render();el("error").textContent=error.message;
  } finally {loading=false;if(el("auto").checked)timer=setTimeout(load,3000);}
}
el("tasks").addEventListener("change",() => load());
el("reload").addEventListener("click",() => load());
el("severity").addEventListener("change",issuesView);el("filter").addEventListener("input",issuesView);
el("auto").addEventListener("change",() => {clearTimeout(timer);if(el("auto").checked)load();});
el("export").addEventListener("click",() => {
  if (!report) return;const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:"application/json"}));
  const a=document.createElement("a");a.href=url;a.download=`jobradar-search-${report.task_id}.json`;a.click();setTimeout(() => URL.revokeObjectURL(url),1000);
});
load();
