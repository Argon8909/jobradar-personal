"use strict";
const byId = id => document.getElementById(id);
const form = byId("chat-form"), input = byId("message"), send = byId("send");
const conversation = byId("conversation"), messages = byId("messages");
let csrf = null, busy = false, nextBefore = null, loadingOlder = false;

async function api(url, options = {}) {
  const response = await fetch(url, {credentials: "same-origin", ...options});
  let body;
  try { body = await response.json(); } catch (_) { throw new Error("Не удалось прочитать ответ приложения."); }
  if (!response.ok) throw new Error(body.error?.message || "Не удалось выполнить запрос.");
  return body;
}
async function sendMessage(text) {
  const response = await fetch("/api/chat", {method:"POST", credentials:"same-origin",
    headers:{"Content-Type":"application/json", "Accept":"application/x-ndjson", "X-JobRadar-CSRF":csrf},
    body:JSON.stringify({message:text})});
  if (!response.ok) {
    const body = await response.json();
    throw new Error(body.error?.message || "Не удалось выполнить запрос.");
  }
  const reader = response.body.getReader(), decoder = new TextDecoder();
  const previews = new Map();
  let buffer = "", completed = false;
  function accept(line) {
    if (!line.trim()) return;
    const event = JSON.parse(line), data = event.data;
    if (event.event === "search_started") byId("status").textContent = "Поиск запущен";
    if (event.event === "search_progress") byId("status").textContent = `Поиск: получено ${data.collected_count}, оценено ${data.evaluated_count}`;
    if (event.event === "search_finished") byId("status").textContent = data.message;
    if (event.event === "search_results") {
      const old = previews.get(data.task_id);
      const unique = [...new Map(data.vacancies.map(item => [item.vacancy_id,item])).values()];
      if (unique.length) {
        const text = `Промежуточные результаты поиска #${data.task_id}\n` + unique.map((item,index) =>
          `${index+1}. ${item.title} — ${item.assessment?.decision || "Оценено"}\n${(item.assessment?.reasoning || "").slice(0,500)}\n${item.url || ""}`).join("\n\n");
        const node = messageNode({role:"assistant",content:text});
        node.dataset.searchPreview = data.task_id;
        if (old?.isConnected) old.replaceWith(node); else messages.append(node);
        previews.set(data.task_id,node);
      } else { old?.remove(); previews.delete(data.task_id); }
    }
    if (event.event === "assessment_progress") byId("status").textContent = data.message;
    if (event.event === "source_action_required") byId("status").textContent = "Откройте окно HH.ru и завершите проверку или вход. Поиск ожидает вашего действия.";
    if (event.event === "source_warning") byId("status").textContent = "Поиск продолжается…";
    if (event.event === "source_resumed") byId("status").textContent = "Продолжаем поиск…";
    if (event.event === "answer") {
      completed = true;
      if (data.error) throw new Error(data.answer);
      previews.forEach(node => node.remove());
    }
  }
  try {
    while (true) {
      const {value, done} = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), {stream:!done});
      const lines = buffer.split("\n"); buffer = lines.pop();
      for (const line of lines) accept(line);
      if (done) { accept(buffer); break; }
    }
    if (!completed) throw new Error("Соединение закрыто до завершения ответа.");
  } finally { reader.releaseLock(); }
}
function showError(message) {
  byId("error-text").textContent = message;
  byId("error").hidden = !message;
}
function controls() {
  input.disabled = busy || !csrf;
  send.disabled = busy || !csrf || !input.value.trim();
  conversation.setAttribute("aria-busy", busy ? "true" : "false");
  byId("status").textContent = busy ? "Ассистент готовит ответ…" : (csrf ? "" : "Подключаемся…");
  document.querySelectorAll("[data-message]").forEach(button => { button.disabled = busy || !csrf; });
}
function messageNode(message) {
  const row = document.createElement("article");
  row.className = `message ${message.role === "user" ? "user" : "assistant"}`;
  if (message.id) row.dataset.messageId = message.id;
  const avatar = document.createElement("span");
  avatar.className = "avatar"; avatar.setAttribute("aria-hidden", "true");
  avatar.textContent = message.role === "user" ? "ВЫ" : "◉";
  const body = document.createElement("div"); body.className = "message-body";
  const label = document.createElement("div"); label.className = "message-label";
  label.textContent = message.role === "user" ? "Вы" : "JobRadar";
  const content = document.createElement("div"); content.className = "message-content";
  // User, resume and model content are text, never executable HTML.
  let position = 0;
  for (const match of message.content.matchAll(/https?:\/\/[^\s<>"']+/g)) {
    const url = match[0].replace(/[),.;!?]+$/, "");
    content.append(document.createTextNode(message.content.slice(position, match.index)));
    const link = document.createElement("a"); link.href = url; link.textContent = url;
    link.target = "_blank"; link.rel = "noopener noreferrer";
    content.append(link); position = match.index + url.length;
  }
  content.append(document.createTextNode(message.content.slice(position)));
  body.append(label, content); row.append(avatar, body);
  return row;
}
function profilesView(profiles) {
  const container = byId("profiles"); container.replaceChildren();
  if (!profiles.length) {
    const note = document.createElement("p"); note.className = "muted";
    note.textContent = "Пока нет профилей. Создайте первый — просто расскажите о себе в чате.";
    container.append(note);
  }
  for (const profile of profiles) {
    const group = document.createElement("div"); group.className = "profile";
    const title = document.createElement("div"); title.className = "profile-name";
    title.textContent = profile.profession || "Профессиональный профиль"; group.append(title);
    for (const context of profile.contexts) {
      const line = document.createElement("div");
      line.className = "profile-context" + (context.is_active ? " active" : "");
      line.textContent = context.name || "Поисковый контекст";
      if (context.is_active) line.setAttribute("aria-current", "true");
      group.append(line);
    }
    container.append(group);
  }
}
async function refresh() {
  const data = await api("/api/state");
  csrf = data.csrf_token;
  profilesView(data.profiles);
  byId("context-title").textContent = data.state.profession || "Общий диалог";
  byId("context-subtitle").textContent = data.state.context_name || "Обсудим ваш опыт и подходящую работу";
  messages.replaceChildren(...data.history.messages.map(messageNode));
  nextBefore = data.history.next_before;
  byId("older").hidden = !data.history.has_more;
  byId("empty").hidden = data.history.messages.length > 0 || data.history.has_more;
  conversation.scrollTop = conversation.scrollHeight;
  conversation.setAttribute("aria-busy", "false"); controls();
}
form.addEventListener("submit", async event => {
  event.preventDefault();
  if (busy || !csrf || !input.value.trim()) return;
  const text = input.value.trim(); busy = true; controls(); showError("");
  byId("empty").hidden = true;
  const pending = messageNode({role:"user", content:text}); pending.classList.add("pending");
  messages.append(pending); conversation.scrollTop = conversation.scrollHeight;
  try {
    await sendMessage(text);
    input.value = ""; input.style.height = "auto";
    await refresh();
  } catch (error) {
    pending.remove();
    showError(error.message + " Обновите историю перед повторной отправкой.");
  } finally { busy = false; controls(); input.focus(); }
});
input.addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault(); form.requestSubmit();
  }
});
input.addEventListener("input", () => {
  input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 175) + "px"; controls();
});
document.querySelectorAll("[data-message]").forEach(button => button.addEventListener("click", () => {
  input.value = button.dataset.message; controls(); input.focus();
}));
byId("refresh").addEventListener("click", async () => {
  if (busy) return;
  try { await refresh(); showError(""); } catch (error) { showError(error.message); }
});
byId("older").addEventListener("click", async () => {
  if (!nextBefore || loadingOlder || busy) return;
  loadingOlder = true; byId("older").disabled = true;
  try {
    const previousHeight = conversation.scrollHeight;
    const page = await api(`/api/history?before=${nextBefore}`);
    messages.prepend(...page.messages.map(messageNode));
    nextBefore = page.next_before; byId("older").hidden = !page.has_more;
    conversation.scrollTop += conversation.scrollHeight - previousHeight;
  } catch (error) { showError(error.message); }
  finally { loadingOlder = false; byId("older").disabled = false; }
});
refresh().catch(error => { showError(error.message); byId("status").textContent = "Нет связи с приложением"; });
