/* Панель использует только локальный API; внешние ресурсы и телеметрия отсутствуют. */
"use strict";
const $ = (id) => document.getElementById(id);
const money = (cents) =>
  new Intl.NumberFormat("ru-RU", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 0,
  }).format(cents / 100);
const exactMoney = (cents) =>
  new Intl.NumberFormat("ru-RU", { style: "currency", currency: "USD" }).format(cents / 100);
const number = (value) => new Intl.NumberFormat("ru-RU").format(value);
const labels = {
  viewed: "Просмотр",
  quoted: "Расчёт стоимости",
  reserved: "Бронирование",
  paid: "Оплата",
  cancelled: "Отмена",
  organic: "Органический",
  search: "Поиск",
  partners: "Партнёры",
  email: "Рассылка",
  camera: "Камеры",
  audio: "Звук",
  lighting: "Свет",
  accepted: "Принято",
  duplicate: "Повтор",
  conflict: "Конфликт",
  invalid: "Невалидно",
  missing_predecessor: "Нет предыдущего этапа",
  identity_mismatch: "Несовпадение идентичности",
  duplicate_payment: "Повторная оплата аренды",
  duplicate_cancellation: "Повторная отмена",
  repeated_stage: "Повтор этапа",
};
const label = (value) => labels[value] || value;
let snapshot = null,
  ledgerPage = 0,
  requestVersion = 0,
  comparisonVersion = 0;
// Экспорт пересчитывается по последним применённым фильтрам, не по несохранённым полям.
let appliedFilters = null;
// Архив — отдельный режим: живые запросы не должны незаметно заменить сохранённые данные.
let archiveMode = null,
  archiveOffset = 0,
  archiveListVersion = 0,
  liveFilters = null;
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function utc(value) {
  return value ? `${value}:00Z` : null;
}
function formFilters() {
  const p = { start: $("start").value, end: $("end").value };
  for (const key of ["channel", "category"]) if ($(key).value) p[key] = $(key).value;
  return p;
}
function query(values) {
  return new URLSearchParams(
    Object.entries(values).filter(([, v]) => v !== null && v !== undefined && v !== ""),
  );
}
async function get(path, params = {}) {
  const response = await fetch(`${path}?${query(params)}`);
  const data = await response.json();
  if (!response.ok)
    throw new Error(typeof data.detail === "string" ? data.detail : "Проверьте параметры запроса.");
  return data;
}
function notice(text, error = false) {
  $("notice").textContent = text;
  $("notice").className = `notice${error ? " error" : ""}`;
}
function fillSelect(id, values) {
  const select = $(id),
    selected = select.value;
  select.replaceChildren(new Option(id === "channel" ? "Все каналы" : "Все категории", ""));
  for (const value of values) select.add(new Option(label(value), value));
  select.value = selected;
}
function countText(count) {
  return `${number(count)} операций`;
}
function setFilterFields(params) {
  $("start").value = params.start.slice(0, 10);
  $("end").value = params.end.slice(0, 10);
  $("as-of").value = params.as_of ? params.as_of.slice(0, 16) : "";
  for (const key of ["channel", "category"]) {
    const value = params[key] || "";
    if (value && !Array.from($(key).options).some((option) => option.value === value)) {
      $(key).add(new Option(label(value), value));
    }
    $(key).value = value;
  }
}
function setReportMode() {
  const frozen = !!archiveMode;
  for (const control of document.querySelectorAll(
    "#filters input, #filters select, #filters button, #compare-form input, #compare-form button",
  ))
    control.disabled = frozen;
  $("report-mode").textContent = frozen
    ? "Архивный снимок · без пересчёта"
    : "Живой отчёт · пересчёт";
  $("report-mode").className = `mode-badge${frozen ? " archived" : ""}`;
  $("archive-live").hidden = !frozen;
  $("archive-banner").hidden = !frozen;
  $("export").textContent = frozen ? "↓ CSV архива" : "↓ Журнал CSV";
  $("export").title = frozen
    ? "Точные сохранённые байты CSV, без пересчёта."
    : "CSV пересчитывается по применённым фильтрам. Новые доставки могут изменить результат.";
  document.querySelector("#quality .section-heading p").textContent = frozen
    ? "Сохранённые счётчики качества на момент захвата истории. Очередь доставок в архив не включена."
    : "Квитанции доставки по всей базе до выбранного момента; фильтры периода и сегмента не применяются.";
}
async function loadArchives(offset = 0) {
  const version = ++archiveListVersion;
  $("archive-list-status").textContent = "Загружаем список архивов…";
  try {
    const data = await get("/api/snapshots", { offset, limit: 20 });
    if (version !== archiveListVersion) return;
    archiveOffset = offset;
    const selected = $("archive-select").value,
      select = $("archive-select");
    select.replaceChildren(
      new Option(data.total ? "Выберите сохранённый снимок" : "Архивов пока нет", ""),
    );
    for (const item of data.items) {
      select.add(
        new Option(
          `${item.created_at.slice(0, 16).replace("T", " ")} UTC · ${item.id.slice(0, 8)} · ${item.parameters.start.slice(0, 10)} — ${item.parameters.end.slice(0, 10)}`,
          item.id,
        ),
      );
    }
    select.value = data.items.some((item) => item.id === selected)
      ? selected
      : data.items[0]?.id || "";
    $("archive-open").disabled = !select.value;
    $("archive-previous").disabled = offset === 0;
    $("archive-next").disabled = offset + 20 >= data.total;
    $("archive-list-status").textContent = data.total
      ? `${offset + 1}–${Math.min(offset + 20, data.total)} из ${data.total} архивов`
      : "Сохранённых снимков ещё нет.";
  } catch (error) {
    if (version === archiveListVersion) $("archive-list-status").textContent = error.message;
  }
}
async function openArchive() {
  const id = $("archive-select").value;
  if (!id) return;
  const version = ++requestVersion;
  comparisonVersion++;
  notice("Открываем сохранённые данные и проверяем контрольные суммы…");
  $("archives").classList.add("loading");
  try {
    const [manifest, data] = await Promise.all([
      get(`/api/snapshots/${id}`),
      get(`/api/snapshots/${id}/report`),
    ]);
    if (version !== requestVersion) return;
    if (!archiveMode)
      liveFilters = appliedFilters
        ? { ...appliedFilters }
        : { ...formFilters(), as_of: utc($("as-of").value) };
    archiveMode = manifest;
    snapshot = data;
    ledgerPage = 0;
    render();
    setFilterFields(manifest.parameters);
    setReportMode();
    $("filters").classList.remove("loading");
    $("archive-id").textContent = manifest.id;
    $("archive-created").textContent =
      `Создан: ${manifest.created_at} · формат ${manifest.format_version} · политика ${manifest.policy_version}`;
    $("archive-parameters").textContent =
      `Период: ${manifest.parameters.start} → ${manifest.parameters.end}, конец не включается. Момент знания: ${manifest.parameters.as_of || "все доставки захваченной истории"}.`;
    $("archive-json-hash").textContent = manifest.report_sha256;
    $("archive-csv-hash").textContent = manifest.csv_sha256;
    $("receipt-results").hidden = true;
    $("comparison").replaceChildren(
      node(
        "p",
        "Открыт архив. Сравнение живых срезов отключено, чтобы не смешивать его с сохранённым результатом. Для пересчёта нажмите «К живому отчёту».",
        "muted",
      ),
    );
    notice(
      `Архив ${manifest.id.slice(0, 8)} · JSON и CSV проверены сервером по SHA-256 · пересчёта нет.`,
    );
  } catch (error) {
    if (version === requestVersion)
      notice(`${error.message}. Ранее открытый отчёт не изменён.`, true);
  } finally {
    $("archives").classList.remove("loading");
  }
}
async function returnToLive() {
  if (!archiveMode) return;
  setFilterFields(liveFilters || archiveMode.parameters);
  await refresh();
}

async function refresh(event) {
  if (event) event.preventDefault();
  const version = ++requestVersion;
  comparisonVersion++;
  notice("Рассчитываем снимок…");
  $("filters").classList.add("loading");
  const filters = { ...formFilters(), as_of: utc($("as-of").value) };
  try {
    const data = await get("/api/report", filters);
    if (version !== requestVersion) return;
    snapshot = data;
    archiveMode = null;
    appliedFilters = filters;
    liveFilters = { ...filters };
    setReportMode();
    ledgerPage = 0;
    render();
    $("receipt-results").hidden = true;
    $("comparison").replaceChildren(
      node("p", "Сравните срезы для текущих фильтров периода и сегмента.", "muted"),
    );
    notice(
      `${number(data.quality.accepted)} принятых событий · политика ${data.policy_version} · время UTC${data.window.as_of ? " · срез " + data.window.as_of.slice(0, 16).replace("T", " ") : " · все известные доставки"}`,
    );
    if (!data.source_event_ids.length)
      notice(
        "Пока нет событий для этого среза. Загрузите синтетический сценарий командой productpulse demo или измените период.",
      );
  } catch (error) {
    if (version !== requestVersion) return;
    const message =
      error.message === "end должен быть позже start"
        ? "Дата окончания должна быть позже даты начала"
        : error.message;
    if (archiveMode) setFilterFields(archiveMode.parameters);
    const previous = archiveMode
      ? ` Показан прежний архив ${archiveMode.id}; пересчёта и изменения сохранённых данных нет.`
      : snapshot
        ? ` Показан последний успешно рассчитанный отчёт: ${snapshot.window.start.slice(0, 10)} — ${snapshot.window.end.slice(0, 10)} (до даты, не включая). Экспорт использует его фильтры и заново рассчитывает данные.`
        : "";
    notice(message + "." + previous, true);
  } finally {
    if (version === requestVersion) $("filters").classList.remove("loading");
  }
}
function render() {
  const m = snapshot.metrics;
  $("net").textContent = money(m.net_cents);
  $("net").title = exactMoney(m.net_cents);
  $("paid").textContent = number(m.paid_rentals);
  $("gross").textContent = money(m.gross_cents);
  $("conversion").textContent = `${m.conversion_pct.toLocaleString("ru-RU")}%`;
  $("reversed").textContent = money(m.reversed_cents);
  $("cancelled").textContent = number(m.cancelled_rentals);
  $("fingerprint").textContent = snapshot.fingerprint;
  $("chart-total").textContent = `Чистая стоимость: ${exactMoney(m.net_cents)}`;
  fillSelect("channel", snapshot.dimensions.channels);
  fillSelect("category", snapshot.dimensions.categories);
  renderChart();
  renderFunnel();
  renderCohorts();
  renderQuality();
  renderLedger();
}
function svgElement(tag, attrs = {}, text) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (text !== undefined) e.textContent = text;
  return e;
}
function renderChart() {
  const box = $("chart");
  box.replaceChildren();
  if (!snapshot.daily.length) {
    box.append(node("div", "В этом периоде нет учитываемых операций.", "empty"));
    return;
  }
  const svg = svgElement("svg", {
      viewBox: "0 0 1000 220",
      role: "img",
      "aria-label": "Дневная стоимость оплат и полных отмен в USD",
    }),
    left = 62,
    right = 985,
    top = 12,
    bottom = 185;
  const start = Date.parse(snapshot.window.start),
    end = Date.parse(snapshot.window.end),
    days = Math.max(1, Math.ceil((end - start) / 86400000)),
    series = new Map(snapshot.daily.map((d) => [d.date, d]));
  const max = Math.max(
    ...snapshot.daily.map((d) => Math.max(d.gross_cents, d.reversed_cents)),
    100,
  );
  for (let i = 0; i <= 4; i++) {
    const y = top + ((bottom - top) * i) / 4;
    svg.append(
      svgElement("line", { x1: left, x2: right, y1: y, y2: y, class: "axis" }),
      svgElement(
        "text",
        { x: left - 10, y: y + 3, "text-anchor": "end" },
        number(Math.round((max * (1 - i / 4)) / 100)),
      ),
    );
  }
  const step = (right - left) / days;
  for (let i = 0; i < days; i++) {
    const date = new Date(start + i * 86400000).toISOString().slice(0, 10),
      row = series.get(date);
    if (row) {
      for (const [j, key, cls] of [
        [0, "gross_cents", "gross-bar"],
        [1, "reversed_cents", "refund-bar"],
      ]) {
        const h = (row[key] / max) * (bottom - top);
        if (!h) continue;
        const rect = svgElement("rect", {
          x: left + i * step + step * (0.12 + j * 0.38),
          y: bottom - h,
          width: Math.max(0.5, step * 0.32),
          height: h,
          rx: 1,
          class: cls,
        });
        rect.append(
          svgElement("title", {}, `${date} · ${j ? "Отмены" : "Оплаты"}: ${exactMoney(row[key])}`),
        );
        svg.append(rect);
      }
    }
    if (i % Math.max(1, Math.ceil(days / 7)) === 0)
      svg.append(
        svgElement(
          "text",
          { x: left + i * step + step / 2, y: 209, "text-anchor": "middle" },
          date.slice(5).split("-").reverse().join("."),
        ),
      );
  }
  box.append(svg);
}
function renderFunnel() {
  const box = $("funnel");
  box.replaceChildren();
  const max = snapshot.funnel[0].sessions || 1;
  for (const [i, stage] of snapshot.funnel.entries()) {
    const row = node("div", undefined, "funnel-row"),
      header = node("div", undefined, "funnel-label"),
      title = node("span", `${String(i + 1).padStart(2, "0")}  ${label(stage.stage)}`);
    if (i) title.append(node("small", `${stage.from_previous_pct ?? 0}% от предыдущего`));
    header.append(title, node("strong", number(stage.sessions)));
    const progress = node("progress");
    progress.max = max;
    progress.value = stage.sessions;
    progress.setAttribute("aria-label", `${label(stage.stage)}: ${stage.sessions}`);
    row.append(header, progress);
    box.append(row);
  }
}
function renderCohorts() {
  const body = $("cohorts");
  body.replaceChildren();
  for (const cohort of snapshot.cohorts) {
    const row = node("tr");
    row.append(
      node("td", cohort.week.slice(5).split("-").reverse().join(".")),
      node("td", number(cohort.users)),
    );
    for (let i = 0; i < 6; i++) {
      const value = cohort.retention[i],
        partial = cohort.observed[i] && !cohort.mature[i],
        cell = node(
          "td",
          value === null ? "—" : `${Math.round(value)}%${partial ? " ◌" : ""}`,
          `heat level-${value === null ? 0 : Math.min(4, Math.ceil(value / 25))}${partial ? " partial" : ""}`,
        );
      cell.title =
        value === null
          ? "Эта неделя ещё не наблюдалась"
          : `${value}% · ${cohort.mature[i] ? "Полная неделя" : "Неполная неделя: показатель ещё может измениться"}`;
      row.append(cell);
    }
    body.append(row);
  }
  if (!snapshot.cohorts.length) {
    const row = node("tr"),
      cell = node("td", "Нет когорт с первой оплатой в периоде.", "empty");
    cell.colSpan = 8;
    row.append(cell);
    body.append(row);
  }
}
function renderQuality() {
  const descriptions = {
      accepted: "Канонические события",
      duplicate: "Повторы не меняют показатели",
      conflict: "Тот же ID, другое содержимое",
      invalid: "JSON или схема не прошли проверку",
    },
    box = $("quality-cards");
  box.replaceChildren();
  for (const status of ["accepted", "duplicate", "conflict", "invalid"]) {
    const button = node("button", undefined, `quality-card ${status}`);
    button.type = "button";
    button.disabled = !!archiveMode;
    if (archiveMode) button.title = "Архив содержит счётчик, но не очередь исходных доставок.";
    button.append(
      node("small", label(status)),
      node("strong", number(snapshot.quality[status])),
      node("p", descriptions[status]),
    );
    button.addEventListener("click", () => showQuality(status));
    box.append(button);
  }
  $("quality-badge").textContent = number(snapshot.quality.conflict + snapshot.quality.invalid);
  const exclusions = $("exclusions");
  exclusions.replaceChildren(node("span", "Семантические исключения до конца периода:"));
  for (const [reason, count] of Object.entries(snapshot.exclusion_counts))
    exclusions.append(node("span", `${label(reason)} · ${count}`, "exclusion-chip"));
  if (!snapshot.exclusions.length) exclusions.append(node("span", "нет", "exclusion-chip"));
}
async function showQuality(status, offset = 0) {
  if (archiveMode) return;
  const box = $("receipt-results");
  box.hidden = false;
  box.replaceChildren(node("p", "Загружаем квитанции…", "muted"));
  try {
    const data = await get("/api/receipts", {
      status,
      as_of: appliedFilters.as_of,
      offset,
      limit: 10,
    });
    box.replaceChildren(node("h3", `${label(status)} · ${data.total}`));
    for (const receipt of data.items) {
      const row = node("div", undefined, "receipt-row"),
        info = node("div"),
        button = node("button", "Исходная доставка →", "button small");
      info.append(
        node("code", receipt.event_id || `Квитанция #${receipt.id}`),
        node(
          "small",
          `${receipt.received_at.slice(0, 19).replace("T", " ")} UTC · ${receipt.reason || "Событие принято"}`,
        ),
      );
      button.addEventListener("click", () => showReceipts([receipt], `Квитанция #${receipt.id}`));
      row.append(info, button);
      box.append(row);
    }
    if (!data.total) box.append(node("p", "Таких доставок в выбранном срезе нет.", "muted"));
    const pager = node("div", undefined, "pagination");
    pager.append(
      node(
        "span",
        `${Math.min(offset + 1, data.total)}–${Math.min(offset + 10, data.total)} из ${data.total}`,
      ),
    );
    const actions = node("div");
    for (const [text, next, disabled] of [
      ["← Назад", offset - 10, offset === 0],
      ["Далее →", offset + 10, offset + 10 >= data.total],
    ]) {
      const b = node("button", text, "button small");
      b.disabled = disabled;
      b.onclick = () => showQuality(status, next);
      actions.append(b);
    }
    pager.append(actions);
    box.append(pager);
  } catch (error) {
    box.replaceChildren(node("p", error.message, "negative"));
  }
}
function renderLedger() {
  const body = $("ledger-body");
  body.replaceChildren();
  const rows = snapshot.ledger.slice(ledgerPage * 20, (ledgerPage + 1) * 20);
  for (const entry of rows) {
    const row = node("tr"),
      first = node("td", entry.rental_id);
    first.append(node("small", entry.event_id));
    row.append(
      first,
      node("td", entry.occurred_at.slice(0, 16).replace("T", " ")),
      node("td", label(entry.channel)),
      node("td", label(entry.category)),
      node("td", exactMoney(entry.amount_cents), entry.amount_cents < 0 ? "negative" : "positive"),
    );
    const cell = node("td"),
      button = node("button", `${entry.source_event_ids.length} события ↗`, "provenance-button");
    button.addEventListener("click", () =>
      trace(entry.source_event_ids, `Аренда ${entry.rental_id}`),
    );
    cell.append(button);
    row.append(cell);
    body.append(row);
  }
  if (!rows.length) {
    const tr = node("tr"),
      td = node("td", "Нет операций в выбранном периоде.", "empty");
    td.colSpan = 6;
    tr.append(td);
    body.append(tr);
  }
  $("ledger-count").textContent = countText(snapshot.ledger.length);
  $("ledger-page").textContent =
    `${snapshot.ledger.length ? ledgerPage * 20 + 1 : 0}–${Math.min((ledgerPage + 1) * 20, snapshot.ledger.length)} из ${snapshot.ledger.length}`;
  $("previous").disabled = ledgerPage === 0;
  $("next").disabled = (ledgerPage + 1) * 20 >= snapshot.ledger.length;
}
async function compareSnapshots(event) {
  if (event) event.preventDefault();
  if (!appliedFilters || archiveMode) return;
  // Ответ старого запроса не должен подменить более новый выбор пользователя.
  const version = ++comparisonVersion,
    box = $("comparison");
  box.replaceChildren(node("p", "Восстанавливаем оба снимка…", "muted"));
  try {
    const params = {
      ...appliedFilters,
      before: utc($("before").value),
      after: utc($("after").value),
    };
    delete params.as_of;
    const data = await get("/api/compare", params);
    if (version !== comparisonVersion) return;
    box.replaceChildren();
    const summary = node("div", undefined, "comparison-baseline");
    summary.append(
      node("span", `Ранний срез · ${exactMoney(data.before.metrics.net_cents)}`),
      node("span", "→"),
      node("span", `Поздний срез · ${exactMoney(data.after.metrics.net_cents)}`),
    );
    box.append(summary);
    const grid = node("div", undefined, "comparison-grid");
    for (const [title, key] of [
      ["Изменение оплат", "gross_cents"],
      ["Изменение отмен", "reversed_cents"],
      ["Изменение чистой стоимости", "net_cents"],
    ]) {
      const stat = node("div", undefined, "compare-stat");
      stat.append(node("small", title), node("strong", signedMoney(data.delta[key])));
      grid.append(stat);
    }
    box.append(grid);
    const revision = data.revision;
    if (!revision) {
      box.append(
        node(
          "p",
          "Для подробного объяснения перезапустите сервер с текущей версией приложения.",
          "panel-note",
        ),
      );
      return;
    }
    box.append(
      node(
        "p",
        "Изменение чистой стоимости = изменение оплат − изменение отмен. Каждая строка ниже вносит точный вклад в эти три суммы.",
        "comparison-note",
      ),
    );
    if (revision.ledger_changes.length && revision.ledger_delta.net_cents === 0) {
      box.append(
        node(
          "p",
          "Чистая разница равна нулю, но операции или их источники изменились. Проверьте состав журнала.",
          "zero-net-note",
        ),
      );
    }
    const ledgerBox = node("div", undefined, "revision-ledger");
    box.append(ledgerBox);
    renderRevisionLedger(data, ledgerBox);
    renderFunnelChanges(data, box);
    box.append(
      node(
        "p",
        `${data.added_event_ids.length} источников вошли в показатели сегмента; ${data.removed_event_ids.length} вышли. Объяснение денежных строк и воронки построено из одной захваченной истории. Изменения когорт отдельно здесь не объясняются.`,
        "panel-note",
      ),
    );
  } catch (error) {
    if (version === comparisonVersion) box.replaceChildren(node("p", error.message, "negative"));
  }
}

function signedMoney(cents) {
  return `${cents > 0 ? "+" : ""}${exactMoney(cents)}`;
}
const revisionReasons = {
  new_delivery: "Новая известная доставка",
  chain_became_valid: "Восстановлена цепочка",
  semantic_reclassification: "Пересмотр допустимости",
  payment_relinked: "Отмена связана с другой оплатой",
  provenance_changed: "Изменились источники",
};
const revisionTypes = { added: "Добавлена", removed: "Исключена", changed: "Изменена" };

function renderRevisionLedger(data, box, page = 0) {
  const changes = data.revision.ledger_changes;
  box.replaceChildren(node("h3", "Из чего сложилась разница"));
  if (!changes.length) {
    box.append(
      node(
        "p",
        "Денежный журнал не изменился. Новые повторы, конфликты или этапы воронки сами по себе не создают денежного вклада.",
        "muted",
      ),
    );
    return;
  }
  const scroll = node("div", undefined, "table-scroll"),
    table = node("table", undefined, "revision-table");
  const head = node("thead"),
    headings = node("tr");
  for (const text of ["Операция / причина", "Δ оплаты", "Δ отмены", "Δ чистая", "Источники"])
    headings.append(node("th", text));
  head.append(headings);
  table.append(head);
  const body = node("tbody");
  for (const change of changes.slice(page * 12, (page + 1) * 12)) {
    const row = node("tr"),
      operation = node("td");
    operation.append(
      node(
        "strong",
        `${revisionTypes[change.change]} ${change.operation_type === "cancellation" ? "отмена" : "оплата"}`,
      ),
      node("code", change.event_id),
      node(
        "small",
        `${change.before ? exactMoney(change.before.amount_cents) : "нет строки"} → ${change.after ? exactMoney(change.after.amount_cents) : "нет строки"}`,
      ),
      node("span", revisionReasons[change.reason], "revision-reason"),
    );
    const evidence = change.evidence;
    if (evidence.before_exclusion || evidence.after_exclusion) {
      operation.append(
        node(
          "small",
          `Исключение: ${evidence.before_exclusion ? label(evidence.before_exclusion) : "нет"} → ${evidence.after_exclusion ? label(evidence.after_exclusion) : "нет"}`,
        ),
      );
    }
    row.append(operation);
    // Вклады приходят целыми центами из единой серверной арифметики, UI их не пересчитывает.
    for (const key of ["gross_cents", "reversed_cents", "net_cents"]) {
      row.append(node("td", signedMoney(change.contribution[key]), "revision-money"));
    }
    const sources = node("td");
    for (const [side, title] of [
      ["before", "До"],
      ["after", "После"],
    ]) {
      if (!change[side]) continue;
      const refs = change[side].source_refs,
        button = node("button", `${title} · ${refs.length} ↗`, "provenance-button");
      button.onclick = () =>
        showRevisionSources(refs, `${title}: ${change.event_id}`, data[side].window.as_of);
      sources.append(button);
    }
    row.append(sources);
    body.append(row);
  }
  table.append(body);
  scroll.append(table);
  box.append(scroll);
  const totals = node(
    "p",
    `Сумма вкладов: оплаты ${signedMoney(data.revision.ledger_delta.gross_cents)} · отмены ${signedMoney(data.revision.ledger_delta.reversed_cents)} · чистая ${signedMoney(data.revision.ledger_delta.net_cents)}`,
    "revision-totals",
  );
  box.append(totals);
  if (changes.length > 12) {
    const pager = node("div", undefined, "pagination"),
      buttons = node("div");
    pager.append(
      node(
        "span",
        `${page * 12 + 1}–${Math.min((page + 1) * 12, changes.length)} из ${changes.length} изменений`,
      ),
    );
    for (const [title, next, disabled] of [
      ["← Назад", page - 1, page === 0],
      ["Далее →", page + 1, (page + 1) * 12 >= changes.length],
    ]) {
      const button = node("button", title, "button small");
      button.disabled = disabled;
      button.onclick = () => renderRevisionLedger(data, box, next);
      buttons.append(button);
    }
    pager.append(buttons);
    box.append(pager);
  }
}

function renderFunnelChanges(data, box) {
  const section = node("div", undefined, "revision-funnel");
  section.append(node("h3", "Воронка — отдельно от денег"));
  if (!data.revision.funnel_changes.length) {
    section.append(node("p", "Количество сессий и их цепочки этапов не изменились.", "muted"));
  }
  for (const change of data.revision.funnel_changes) {
    const row = node("div", undefined, "funnel-change-row"),
      info = node("div");
    info.append(
      node("strong", label(change.stage)),
      node(
        "small",
        `${change.before_sessions} → ${change.after_sessions} сессий · разница ${change.delta_sessions > 0 ? "+" : ""}${change.delta_sessions}${change.changed_session_ids.length ? ` · изменены источники у ${change.changed_session_ids.length}` : ""}`,
      ),
    );
    const buttons = node("div", undefined, "funnel-source-buttons");
    for (const [side, title] of [
      ["before", "До"],
      ["after", "После"],
    ]) {
      const refs = change[`${side}_source_refs`];
      if (!refs.length) continue;
      const button = node("button", `${title} · ${refs.length} источн. ↗`, "provenance-button");
      button.onclick = () =>
        showRevisionSources(refs, `${title}: ${label(change.stage)}`, data[side].window.as_of);
      buttons.append(button);
    }
    row.append(info, buttons);
    section.append(row);
  }
  box.append(section);
}

function showRevisionSources(refs, title, asOf, offset = 0) {
  $("details-title").textContent = title;
  const body = $("details-body");
  body.replaceChildren(
    node(
      "p",
      `Канонические источники среза ${asOf}. Исходные события не удаляются при изменении проекции.`,
      "muted",
    ),
  );
  for (const ref of refs.slice(offset, offset + 20)) {
    const article = node("article", undefined, "trace-item");
    article.append(
      node("h3", `${label(ref.event_type)} · квитанция #${ref.receipt_id}`),
      node("p", ref.event_id, "trace-meta"),
      node("p", `Событие: ${ref.occurred_at}`, "trace-meta"),
      node("p", `Получено: ${ref.received_at} · источник: ${ref.source}`, "trace-meta"),
    );
    const button = node("button", "Исходная доставка →", "button small");
    button.onclick = async () => {
      try {
        const data = await get("/api/receipts", {
          event_id: ref.event_id,
          status: "accepted",
          as_of: asOf,
        });
        const receipt = data.items.find((item) => item.id === ref.receipt_id);
        if (!receipt) throw new Error("Каноническая квитанция не найдена в указанном срезе.");
        showReceipts([receipt], `${title} · квитанция #${ref.receipt_id}`);
      } catch (error) {
        body.append(node("p", error.message, "negative"));
      }
    };
    article.append(button);
    body.append(article);
  }
  if (refs.length > 20) {
    const pager = node("div", undefined, "pagination"),
      buttons = node("div");
    pager.append(
      node("span", `${offset + 1}–${Math.min(offset + 20, refs.length)} из ${refs.length}`),
    );
    for (const [text, next, disabled] of [
      ["← Назад", offset - 20, offset === 0],
      ["Далее →", offset + 20, offset + 20 >= refs.length],
    ]) {
      const button = node("button", text, "button small");
      button.disabled = disabled;
      button.onclick = () => showRevisionSources(refs, title, asOf, next);
      buttons.append(button);
    }
    pager.append(buttons);
    body.append(pager);
  }
  if (!$("details").open) $("details").showModal();
}

async function trace(
  ids,
  title,
  asOf = archiveMode ? archiveMode.parameters.as_of : appliedFilters.as_of,
) {
  $("details-title").textContent = title;
  $("details-body").replaceChildren(node("p", "Читаем исходные квитанции…", "muted"));
  if (!$("details").open) $("details").showModal();
  try {
    const groups = await Promise.all(
      ids.map((event_id) =>
        get("/api/receipts", {
          event_id,
          as_of: asOf,
          limit: 200,
          status: archiveMode ? "accepted" : null,
        }),
      ),
    );
    showReceipts(
      groups
        .flatMap((g) => g.items)
        .sort(
          (a, b) =>
            (a.payload?.occurred_at || "").localeCompare(b.payload?.occurred_at || "") ||
            a.id - b.id,
        ),
      title,
    );
  } catch (error) {
    $("details-body").replaceChildren(node("p", error.message, "negative"));
  }
}
function showReceipts(receipts, title) {
  $("details-title").textContent = title;
  const body = $("details-body");
  body.replaceChildren(
    node(
      "p",
      archiveMode
        ? "Это канонические квитанции из текущей базы, а не содержимое архива. В архиве сохранены только ссылки на события."
        : "Сохранены исходный JSON, источник, время получения и SHA-256. Повторы и конфликты не заменяют принятое событие.",
      "muted",
    ),
  );
  for (const r of receipts) {
    const item = node("article", undefined, "trace-item");
    item.append(
      node(
        "h3",
        `${r.payload ? label(r.payload.event_type) : "Некорректная доставка"} · ${label(r.status)}`,
      ),
    );
    item.append(node("p", `ID: ${r.event_id || "—"} · квитанция #${r.id}`, "trace-meta"));
    if (r.payload)
      item.append(
        node(
          "p",
          `Событие: ${r.payload.occurred_at} · ${r.payload.user_id} / ${r.payload.session_id}`,
          "trace-meta",
        ),
      );
    item.append(node("p", `Получено: ${r.received_at} · источник: ${r.source}`, "trace-meta"));
    if (r.reason) item.append(node("p", r.reason, "trace-meta"));
    item.append(node("p", `SHA-256 исходной строки: ${r.raw_sha256}`, "digest"));
    const details = node("details"),
      summary = node("summary", "Исходный JSON"),
      pre = node("pre", r.raw_json);
    details.append(summary, pre);
    item.append(details);
    body.append(item);
  }
  if (!receipts.length) body.append(node("p", "Квитанции не найдены в этом срезе.", "empty"));
  if (!$("details").open) $("details").showModal();
}
$("filters").addEventListener("submit", refresh);
$("compare-form").addEventListener("submit", compareSnapshots);
$("previous").onclick = () => {
  ledgerPage--;
  renderLedger();
};
$("next").onclick = () => {
  ledgerPage++;
  renderLedger();
};
$("close-dialog").onclick = () => $("details").close();
$("details").addEventListener("click", (e) => {
  if (e.target === $("details")) {
    const r = e.target.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom)
      e.target.close();
  }
});
$("export").onclick = () => {
  if (archiveMode) window.location.href = `/api/snapshots/${archiveMode.id}/ledger.csv`;
  else if (appliedFilters) window.location.href = `/api/export?${query(appliedFilters)}`;
};
$("archive-json").onclick = () => {
  if (archiveMode) window.location.href = `/api/snapshots/${archiveMode.id}/report`;
};
$("archive-open").onclick = openArchive;
$("archive-live").onclick = returnToLive;
$("archive-refresh").onclick = () => loadArchives(0);
$("archive-previous").onclick = () => loadArchives(Math.max(0, archiveOffset - 20));
$("archive-next").onclick = () => loadArchives(archiveOffset + 20);
$("archive-select").onchange = () => {
  $("archive-open").disabled = !$("archive-select").value;
};
loadArchives();
for (const a of document.querySelectorAll("nav a"))
  a.onclick = () => {
    document.querySelector("nav a.active")?.classList.remove("active");
    a.classList.add("active");
  };
refresh().then(() => {
  if (snapshot) compareSnapshots();
});
