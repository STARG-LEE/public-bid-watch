(() => {
  const DATA_URL = "./data/index.json";

  const state = {
    tab: "closing_soon",
    query: "",
    selectedKeyword: "__all__",
    selectedSource: "__all__",   // __all__ | g2b | bizinfo
    data: null,
  };

  const SOURCE_LABEL = { g2b: "나라장터", bizinfo: "기업마당", iris: "IRIS", nrf: "NRF", iitp: "IITP" };

  const $ = (sel) => document.querySelector(sel);

  function fmtKoreanDateTime(iso) {
    if (!iso) return "-";
    try {
      const d = new Date(iso);
      if (isNaN(d)) return iso;
      const y = d.getFullYear();
      const mo = String(d.getMonth() + 1).padStart(2, "0");
      const da = String(d.getDate()).padStart(2, "0");
      const hh = String(d.getHours()).padStart(2, "0");
      const mm = String(d.getMinutes()).padStart(2, "0");
      return `${y}-${mo}-${da} ${hh}:${mm}`;
    } catch {
      return iso;
    }
  }

  function fmtRemaining(hours) {
    if (hours === null || hours === undefined) return { text: "마감일 미상", cls: "expired" };
    if (hours < 0) return { text: "마감됨", cls: "expired" };
    if (hours < 1) return { text: `${Math.round(hours * 60)}분 남음`, cls: "danger" };
    if (hours < 24) return { text: `${hours.toFixed(1)}시간 남음`, cls: "danger" };
    const days = hours / 24;
    if (days < 3) return { text: `${days.toFixed(1)}일 남음`, cls: "warn" };
    return { text: `${days.toFixed(0)}일 남음`, cls: "ok" };
  }

  function fmtKrw(s) {
    if (!s) return "";
    const n = Number(String(s).replace(/[^\d.-]/g, ""));
    if (!isFinite(n) || n <= 0) return "";
    if (n >= 1e8) return `${(n / 1e8).toFixed(1)}억`;
    if (n >= 1e4) return `${(n / 1e4).toFixed(0)}만`;
    return n.toLocaleString();
  }

  function buildBidUrl(item) {
    if (item.url) return item.url;                       // bizinfo 정규화 필드
    if (item.bidNtceDtlUrl) return item.bidNtceDtlUrl;   // g2b
    if (item.bidNtceNo) {
      return `https://www.g2b.go.kr:8101/ep/invitation/publish/bidInfoDtl.do?bidno=${encodeURIComponent(item.bidNtceNo)}&bidseq=${encodeURIComponent(item.bidNtceOrd || "")}`;
    }
    return "#";
  }

  function cardHtml(item) {
    const hours = item._hours_remaining;
    const remaining = fmtRemaining(hours);
    const url = buildBidUrl(item);
    const source = item.source || "g2b";
    const title = item.title || item.bidNtceNm || "(제목 없음)";
    const org = item.org || item.ntceInsttNm || item.dminsttNm || "";
    const demander = source === "g2b"
      ? (item.dminsttNm && item.dminsttNm !== item.ntceInsttNm ? item.dminsttNm : "")
      : (item.exec_org && item.exec_org !== item.org ? item.exec_org : "");
    const budget = fmtKrw(item.asignBdgtAmt) || fmtKrw(item.presmptPrce);
    const method = [item.bidMethdNm, item.cntrctCnclsMthdNm].filter(Boolean).join(" / ");
    const kind = item.ntceKindNm && item.ntceKindNm !== "일반" ? item.ntceKindNm : "";
    const matched = Array.isArray(item.matched_keywords) ? item.matched_keywords : [];
    const srvceDiv = item.srvceDivNm || "";
    const divCls = srvceDiv === "기술용역" ? "tech" : srvceDiv === "일반용역" ? "general" : "";
    const category = item.category || "";
    const score = item._relevance_score;
    const reason = item._relevance_reason || "";
    const scoreCls = score >= 4 ? "high" : score >= 3 ? "mid" : "low";
    const scoreHtml = (typeof score === "number")
      ? `<span class="score-badge ${scoreCls}" title="${escapeHtml(reason)}">★${score}</span>`
      : "";
    const sourceLabel = SOURCE_LABEL[source] || source;
    const sourceHtml = `<span class="source-badge src-${source}">${escapeHtml(sourceLabel)}</span>`;
    const closeDtDisplay = item.bidClseDt || item.close_dt_raw || "";
    const idDisplay = source === "g2b"
      ? `${escapeHtml(item.bidNtceNo || "")}${item.bidNtceOrd ? "-" + escapeHtml(item.bidNtceOrd) : ""}`
      : escapeHtml(item.source_id || "");

    let cardCls = "card";
    if (remaining.cls === "danger" || remaining.cls === "warn") cardCls += " soon";
    if (remaining.cls === "expired" && hours !== null && hours < 0) cardCls += " overdue";

    const matchedHtml = matched.length
      ? `<div class="matched-keywords">${matched.map(k => `<span class="kw">${escapeHtml(k)}</span>`).join("")}</div>`
      : "";

    const demanderLabel =
      source === "g2b" ? "수요기관" :
      source === "iris" ? "소관부처" :
      (source === "nrf" || source === "iitp") ? "" : "수행기관";
    const orgLabel =
      source === "g2b" ? "공고기관" :
      source === "iris" ? "주관기관" :
      (source === "nrf" || source === "iitp") ? "기관" : "소관기관";
    const idLabel = source === "g2b" ? "공고번호" : "공고ID";

    return `
      <article class="${cardCls}">
        <h3 class="card-title"><a href="${url}" target="_blank" rel="noopener">${escapeHtml(title)}</a></h3>
        <div class="card-meta">
          ${sourceHtml}
          ${scoreHtml}
          ${srvceDiv ? `<span class="div-badge ${divCls}">${escapeHtml(srvceDiv)}</span>` : ""}
          ${category && source === "bizinfo" ? `<span class="div-badge cat">${escapeHtml(category)}</span>` : ""}
          ${kind ? `<span class="tag">${escapeHtml(kind)}</span>` : ""}
          ${org ? `<span><strong>${orgLabel}</strong> ${escapeHtml(org)}</span>` : ""}
          ${demander ? `<span><strong>${demanderLabel}</strong> ${escapeHtml(demander)}</span>` : ""}
          ${method ? `<span><strong>입찰방식</strong> ${escapeHtml(method)}</span>` : ""}
          ${idDisplay ? `<span><strong>${idLabel}</strong> ${idDisplay}</span>` : ""}
        </div>
        ${reason ? `<div class="relevance-reason">${escapeHtml(reason)}</div>` : ""}
        ${matchedHtml}
        <div class="card-bottom">
          <span class="remaining ${remaining.cls}">${remaining.text}</span>
          <span class="budget">
            ${closeDtDisplay ? `마감 <strong>${escapeHtml(closeDtDisplay)}</strong>` : ""}
            ${budget ? ` · 예산 <strong>${budget}원</strong>` : ""}
          </span>
        </div>
      </article>
    `;
  }

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function render() {
    const listEl = $("#list");
    if (!state.data) {
      listEl.innerHTML = '<p class="empty">데이터를 불러오는 중…</p>';
      return;
    }

    const bucket = state.data[state.tab] || [];
    const q = state.query.trim().toLowerCase();
    const kw = state.selectedKeyword;
    const src = state.selectedSource;

    const filtered = bucket.filter((it) => {
      if (src && src !== "__all__") {
        if ((it.source || "g2b") !== src) return false;
      }
      if (kw && kw !== "__all__") {
        const matched = Array.isArray(it.matched_keywords) ? it.matched_keywords : [];
        if (!matched.includes(kw)) return false;
      }
      if (q) {
        const hay = [
          it.title, it.bidNtceNm,
          it.org, it.ntceInsttNm, it.dminsttNm, it.exec_org,
          it.bsnsDivNm, it.summary, it.category,
        ].filter(Boolean).join(" ").toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    if (filtered.length === 0) {
      const parts = [];
      if (kw && kw !== "__all__") parts.push(`키워드 "${escapeHtml(kw)}"`);
      if (q) parts.push(`"${escapeHtml(state.query)}"`);
      const msg = parts.length
        ? `${parts.join(" + ")} 에 매치되는 공고가 없습니다.`
        : "해당하는 공고가 없습니다.";
      listEl.innerHTML = `<p class="empty">${msg}</p>`;
      return;
    }

    listEl.innerHTML = filtered.map(cardHtml).join("");
  }

  function buildKeywordFilter() {
    const container = $("#kw-filter");
    if (!state.data || !container) return;

    // 현재 탭 + 출처 필터 적용된 부분집합에서 키워드 카운트
    const src = state.selectedSource;
    const bucket = (state.data[state.tab] || []).filter((it) =>
      src === "__all__" ? true : (it.source || "g2b") === src,
    );
    const counts = new Map();
    for (const it of bucket) {
      const kws = Array.isArray(it.matched_keywords) ? it.matched_keywords : [];
      for (const k of kws) counts.set(k, (counts.get(k) || 0) + 1);
    }

    // 설정된 키워드 순서 유지 + 실제 매치된 것만 표시
    const configKws = state.data.config?.keywords || [];
    const ordered = configKws.filter((k) => counts.has(k));
    // 설정에 없지만 데이터에 있는 키워드도 뒤에 추가 (예: 설정 변경 직후)
    for (const [k] of counts) if (!ordered.includes(k)) ordered.push(k);

    const chips = [`<button class="kw-chip ${state.selectedKeyword === "__all__" ? "active" : ""}" data-kw="__all__">전체 <span class="count">${bucket.length}</span></button>`];
    for (const k of ordered) {
      const active = state.selectedKeyword === k ? "active" : "";
      chips.push(`<button class="kw-chip ${active}" data-kw="${escapeHtml(k)}">${escapeHtml(k)} <span class="count">${counts.get(k)}</span></button>`);
    }
    container.innerHTML = chips.join("");
  }

  function updateMeta() {
    const d = state.data;
    if (!d) return;
    $("#generated-at").textContent = `갱신: ${fmtKoreanDateTime(d.generated_at)}`;
    const kws = (d.config?.keywords || []).join(", ") || "없음";
    const mode = d.config?.match_mode === "all" ? "(모두 포함)" : "(하나라도 포함)";
    $("#keywords").textContent = `키워드 ${mode}: ${kws}`;
    $("#stat-soon").textContent = d.stats?.closing_soon ?? "-";
    $("#stat-open").textContent = d.stats?.open ?? "-";
    $("#stat-closed").textContent = d.stats?.closed ?? "-";
    $("#stat-total").textContent = d.stats?.total ?? "-";
    updateSourceCounts();
  }

  // 출처 칩 카운트를 '현재 탭의 버킷' 기준으로 갱신.
  // 위 카운트와 실제 클릭 시 보이는 항목 수가 정확히 일치하도록.
  function updateSourceCounts() {
    const d = state.data;
    if (!d) return;
    const bucket = d[state.tab] || [];
    const counts = { g2b: 0, bizinfo: 0, iris: 0, nrf: 0, iitp: 0 };
    for (const it of bucket) {
      const s = it.source || "g2b";
      if (s in counts) counts[s] += 1;
    }
    const setCount = (sel, n) => {
      const el = $(sel);
      if (el) el.textContent = n;
    };
    setCount("#cnt-all", bucket.length);
    setCount("#cnt-g2b", counts.g2b);
    setCount("#cnt-bizinfo", counts.bizinfo);
    setCount("#cnt-iris", counts.iris);
    setCount("#cnt-nrf", counts.nrf);
    setCount("#cnt-iitp", counts.iitp);
  }

  function bindEvents() {
    document.querySelectorAll(".tab").forEach((btn) => {
      btn.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.tab = btn.dataset.tab;
        // 탭 바꾸면 출처 카운트 + 키워드 칩 둘 다 현재 버킷 기준으로 재계산
        updateSourceCounts();
        buildKeywordFilter();
        render();
      });
    });
    $("#search").addEventListener("input", (e) => {
      state.query = e.target.value;
      render();
    });
    // 출처 칩 클릭
    $("#source-filter").addEventListener("click", (e) => {
      const chip = e.target.closest(".source-chip");
      if (!chip) return;
      state.selectedSource = chip.dataset.source;
      // 출처 바꾸면 키워드 선택은 초기화 (잘못된 조합 방지)
      state.selectedKeyword = "__all__";
      document.querySelectorAll("#source-filter .source-chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      buildKeywordFilter();
      render();
    });
    // 키워드 칩 클릭은 이벤트 위임으로 처리 (칩이 동적으로 재생성되므로)
    $("#kw-filter").addEventListener("click", (e) => {
      const chip = e.target.closest(".kw-chip");
      if (!chip) return;
      state.selectedKeyword = chip.dataset.kw;
      document.querySelectorAll("#kw-filter .kw-chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      render();
    });
  }

  async function load() {
    try {
      const r = await fetch(DATA_URL, { cache: "no-store" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      state.data = await r.json();
      updateMeta();
      buildKeywordFilter();
      render();
    } catch (e) {
      $("#list").innerHTML = `<p class="empty">데이터를 불러오지 못했습니다: ${escapeHtml(String(e))}<br>워크플로우가 한 번 이상 실행되어야 <code>docs/data/index.json</code>이 생성됩니다.</p>`;
    }
  }

  bindEvents();
  load();
})();
