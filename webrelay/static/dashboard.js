const el = id => document.getElementById(id);
const time = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
let token = "";
let selected = "";
let pending = false;
let polling = false;
let snapshot = "";
let picture = "";
let loadSequence = 0;

function node(tag, text, className) {
  const result = document.createElement(tag);
  result.textContent = text;
  if (className) result.className = className;
  return result;
}

function button(text, callback, className) {
  const result = node("button", text, className);
  result.type = "button";
  result.onclick = callback;
  return result;
}

async function api(route, { method = "GET", body, key, binary = false } = {}) {
  const response = await fetch("/api/" + route, {
    method, headers: { Authorization: "Bearer " + token, ...(body ? { "Content-Type": "application/json" } : {}),
      ...(key ? { "Idempotency-Key": key } : {}) },
    body: body ? JSON.stringify(body) : undefined
  });
  if (response.ok) return binary ? response.blob() : response.json();
  const payload = await response.json();
  throw new Error(typeof payload.detail === "string" ? payload.detail.replaceAll("_", " ") : "Check the submitted fields.");
}

async function refresh() {
  const revision = ++loadSequence;
  const jobs = await api("jobs");
  if (revision !== loadSequence || !token) return;
  const current = JSON.stringify([selected, jobs]);
  if (current !== snapshot) {
    snapshot = current;
    render(jobs);
  }
}

async function act(callback) {
  if (pending) return;
  pending = true;
  loadSequence += 1;
  el("status").textContent = "";
  try { await callback(); await refresh(); }
  catch (error) { el("status").textContent = error.message; }
  finally { pending = false; }
}

function releasePicture() {
  if (picture) URL.revokeObjectURL(picture);
  picture = "";
}

function download(job, format) {
  void act(async () => {
    const blob = await api("jobs/" + job.id + "/report?format=" + format, { binary: true });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "webrelay-" + job.id + "." + format;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
}

function render(jobs) {
  releasePicture();
  const job = jobs.find(item => item.id === selected) ?? jobs[0];
  const panel = el("job");
  el("jobs").replaceChildren(...jobs.map(item => {
    const row = button(item.payload.instruction + " · " + item.status, () => { selected = item.id; render(jobs); }, "row");
    row.setAttribute("aria-pressed", String(item.id === job?.id));
    return row;
  }));
  if (!job) {
    el("jobs").append(node("p", "No scheduled jobs."));
    panel.replaceChildren(node("p", "Select a job."));
    return;
  }
  panel.replaceChildren(node("p", job.status + " · " + job.attempts + " attempts"),
    node("p", "Scheduled " + time.format(new Date(job.run_at * 1000))));
  if (job.error) panel.append(node("p", "Last error · " + job.error.replaceAll("_", " ")));
  if (["queued", "retrying", "running"].includes(job.status)) {
    panel.append(button("Cancel job", () => void act(() => api("jobs/" + job.id + "/cancel", { method: "POST" })), "action"));
  }
  const timeline = node("ol", "");
  for (const event of job.history) {
    timeline.append(node("li", event.event.replaceAll("_", " ") + " · " + time.format(new Date(event.at * 1000)) +
      (event.code ? " · " + event.code.replaceAll("_", " ") : "")));
  }
  panel.append(node("h3", "Timeline"), timeline);
  if (!job.result) return;
  panel.append(node("h3", "Extracted rows"), node("p", job.result.source + " · " + job.result.duration_ms + " ms"),
    button("Download CSV", () => download(job, "csv"), "action"), button("Download JSON", () => download(job, "json"), "action"));
  const rows = job.result.rows;
  if (rows.length) {
    const container = node("div", "", "table");
    const table = document.createElement("table");
    const fields = Object.keys(rows[0]);
    const headings = table.createTHead().insertRow();
    for (const field of fields) headings.append(node("th", field));
    const body = table.createTBody();
    for (const row of rows) {
      const line = body.insertRow();
      for (const field of fields) line.append(node("td", row[field]));
    }
    container.append(table);
    panel.append(container);
  } else panel.append(node("p", "No rows matched the filter."));
  const image = document.createElement("img");
  image.alt = "Captured sample webpage";
  void api("jobs/" + job.id + "/screenshot", { binary: true }).then(blob => {
    if (!image.isConnected) return;
    picture = URL.createObjectURL(blob);
    image.src = picture;
  }).catch(() => image.replaceWith(node("p", "Screenshot unavailable.")));
  panel.append(node("h3", "Screenshot"), image);
}

async function login(value) {
  token = value;
  try {
    const templates = await api("templates");
    el("templates").replaceChildren(...templates.map(template => button(template.title, () => {
      el("create").elements.instruction.value = "Collect a " + template.title.toLowerCase() + " report.";
    })));
    await refresh();
    el("connect").hidden = true;
    el("workspace").hidden = false;
    el("logout").hidden = false;
  } catch (error) {
    token = "";
    el("status").textContent = error.message;
  }
}
el("connect").onsubmit = event => {
  event.preventDefault();
  const value = new FormData(el("connect")).get("token");
  el("connect").reset();
  void login(value);
};
el("logout").onclick = () => {
  if (pending) return;
  token = "";
  selected = "";
  snapshot = "";
  loadSequence += 1;
  releasePicture();
  el("connect").hidden = false;
  el("workspace").hidden = true;
  el("logout").hidden = true;
  el("jobs").replaceChildren();
  el("job").replaceChildren();
  el("create").reset();
};
el("create").onsubmit = event => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(el("create")));
  void act(async () => {
    const job = await api("jobs", { method: "POST", key: crypto.randomUUID(), body: {
      instruction: values.instruction, search: values.search,
      scheduled_at: values.scheduled_at ? new Date(values.scheduled_at).toISOString() : null,
      simulate_first_failure: values.simulate_first_failure === "on"
    } });
    selected = job.id;
  });
};
const config = await api("config");
if (config.demo) {
  el("demo").hidden = false;
  await login("webrelay-local-demo");
}
setInterval(async () => {
  if (!token || pending || polling) return;
  polling = true;
  try { await refresh(); }
  catch { return; }
  finally { polling = false; }
}, 1000);
