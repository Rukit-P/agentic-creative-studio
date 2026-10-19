"use strict";
const $ = (id) => document.getElementById(id);
const AGENT_LABEL = {planner: "Planner", copywriter: "Copywriter", art_director: "Art director", critic: "Critic", packager: "Packager"};
let current = null, stream = null, tasks = [];

function el(tag, props = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v; else if (k === "style") node.style.cssText = v; else node.setAttribute(k, v);
  }
  for (const kid of kids) node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return node;
}

async function api(path, options) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

const time = (t) => new Date(t * 1000).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});

function renderList() {
  const list = $("task-list");
  list.replaceChildren(...tasks.map((t) => {
    const b = el("button", {type: "button", "aria-current": String(t.id === current)},
      el("strong", {}, t.title), el("span", {class: "state s-" + t.state}, t.state),
      el("small", {}, `${t.progress}/${t.total} steps · ${time(t.created)}${t.chaos !== "none" ? " · drill: " + t.chaos : ""}`));
    b.addEventListener("click", () => select(t.id));
    return el("li", {}, b);
  }));
}

function renderResult(task) {
  const box = $("result");
  if (!task.result) { box.hidden = true; box.replaceChildren(); return; }
  const r = task.result;
  box.hidden = false;
  box.className = "result";
  box.replaceChildren(
    el("div", {}, el("h4", {}, "Concept"), el("p", {class: "concept"}, r.concept)),
    el("div", {class: "grid2"},
      el("div", {}, el("h4", {}, "Headlines"), el("ol", {class: "heads"}, ...r.headlines.map((h) => el("li", {}, h))),
        el("h4", {style: "margin-top:14px"}, "Call to action"), el("p", {}, r.cta)),
      el("div", {}, el("h4", {}, `Palette · ${r.palette.name}`),
        el("div", {class: "swatches"}, ...r.palette.colours.map((c) => el("div", {class: "sw"}, el("i", {style: "background:" + c}), el("span", {}, c)))),
        el("h4", {style: "margin-top:14px"}, "Typography"), el("p", {}, `${r.typography.heading} / ${r.typography.body}`))),
    el("div", {class: "grid2"},
      el("div", {}, el("h4", {}, "Shot list"), el("ol", {class: "heads"}, ...r.shot_list.map((s) => el("li", {}, s)))),
      el("div", {}, el("h4", {}, "Critic (rule-based)"), el("div", {class: "score"}, r.score + "/100"),
        el("ul", {class: "checks"}, ...r.checks.map((c) => el("li", {}, (c.passed ? "✓ " : "✗ ") + c.rule))))),
    r.export ? el("p", {class: "muted"}, r.export.status === "saved" ? `Export approved and saved: ${r.export.saved} (${r.export.bytes} bytes)`
      : `Export ${r.export.status}${r.export.error ? ": " + r.export.error : ""}`) : "");
}

function render(task) {
  $("empty").hidden = true; $("detail").hidden = false;
  $("d-title").textContent = task.brief.title;
  $("d-sub").textContent = `${task.provider} provider · run ${task.runs || 0} · ${task.id}`;
  const state = $("d-state"); state.textContent = task.state; state.className = "state s-" + task.state;
  $("btn-cancel").disabled = !["queued", "running", "awaiting_approval"].includes(task.state);
  const pending = task.state === "awaiting_approval" && task.approval;
  $("approval").hidden = !pending;
  if (pending) $("a-summary").textContent = `${task.approval.tool}: ${task.approval.summary}. Nothing is written until you approve.`;
  const m = task.metrics || {};
  $("metrics").replaceChildren(...[["Attempts", m.attempts], ["Retries", m.retries], ["Model calls", m.model_calls],
    ["Tokens", (m.input_tokens || 0) + (m.output_tokens || 0)], ["Model time", Math.round(m.model_ms || 0) + " ms"],
    ["Tool calls", m.tool_calls], ["Blocked", m.tools_blocked], ["Invalid answers", m.invalid_outputs]]
    .map(([k, v]) => el("div", {}, el("dt", {}, k), el("dd", {}, v ?? 0))));
  $("btn-retry").disabled = !["failed", "cancelled"].includes(task.state);
  $("steps").replaceChildren(...task.steps.map((s) => el("li", {class: "step s-" + s.state},
    el("div", {class: "name"}, AGENT_LABEL[s.agent] || s.agent),
    el("div", {class: "st s-" + s.state}, s.state),
    el("div", {class: "att"}, s.attempts ? `${s.attempts} attempt${s.attempts > 1 ? "s" : ""}` : "not started"),
    s.error ? el("div", {class: "att"}, s.error) : "",
    s.ms ? el("div", {class: "att"}, `${Math.round(s.ms)} ms${s.tokens ? " · " + s.tokens + " tok" : ""}`) : "")));
  renderResult(task);
  $("events").replaceChildren(...task.events.map((e) =>
    el("li", {class: /failed|blocked|rejected/.test(e.type) ? "bad" : /succeeded|approved/.test(e.type) ? "ok" : ""},
      `${time(e.time)}  ${e.type.padEnd(18)} ${e.message}`)));
  $("events").scrollTop = $("events").scrollHeight;
}

async function refreshList() { tasks = await api("/api/tasks"); renderList(); }

async function select(id) {
  current = id;
  if (stream) stream.close();
  const task = await api("/api/tasks/" + id);
  render(task); renderList();
  {
    stream = new EventSource(`/api/tasks/${id}/events`);
    stream.addEventListener("task", async () => {
      if (current !== id) return;
      const fresh = await api("/api/tasks/" + id);
      render(fresh); refreshList();
      if (["succeeded", "failed", "cancelled"].includes(fresh.state)) { stream.close(); }
    });
    stream.onerror = () => stream && stream.close();
  }
}

$("brief-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = $("form-error"); error.hidden = true;
  try {
    const task = await api("/api/tasks", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
      brief: {title: $("f-title").value, text: $("f-text").value, audience: $("f-audience").value, tone: $("f-tone").value,
        reference: $("f-reference").value, export: $("f-export").checked},
      chaos: $("f-chaos").value})});
    await refreshList(); await select(task.id);
  } catch (e) { error.textContent = e.message; error.hidden = false; }
});

for (const [id, action] of [["btn-cancel", "cancel"], ["btn-retry", "retry"], ["btn-approve", "approve"], ["btn-reject", "reject"]]) {
  $(id).addEventListener("click", async () => {
    try { await api(`/api/tasks/${current}/${action}`, {method: "POST"}); await select(current); }
    catch (e) { $("form-error").textContent = e.message; $("form-error").hidden = false; }
  });
}

(async () => {
  const health = await api("/api/health");
  $("provider").textContent = health.provider === "demo" ? "demo · offline" : health.provider;
  $("version").textContent = "v" + health.version;
  await refreshList();
  if (tasks.length) select(tasks[0].id);
})();
