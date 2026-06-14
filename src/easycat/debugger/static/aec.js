"use strict";
// EasyCat debugger AEC view — classic (non-module) script.
//
// Exposes ``window.EasyCatAec = { renderAecView }``, extracted verbatim from
// the host SPA's inline script so ``index.html`` stays lean.  The host page
// loads this before its inline script (see the ``<script src>`` in <head>);
// the render functions resolve the host helpers (``el``, ``$``, ``clear``,
// ``getJSON``, ``postJSON``, ``state``) from the shared classic-script global
// scope at call time — exactly as they did inline.
//
// SECURITY: this file shares the host SPA's NO-innerHTML invariant.  Every
// node is built with the host's ``el()`` helper (textContent + URL-sanitised
// attributes) or ``document.createElement`` for canvases; nothing here ever
// touches ``innerHTML`` / ``outerHTML`` / ``insertAdjacentHTML``.  All visual
// content is drawn into a ``<canvas>`` 2D context or set as element styles.
(function (global) {
  // ── AEC view styles ──────────────────────────────────────────────
  // Extracted out of index.html's inline <style>.  Injected once via a
  // <style> element whose ``textContent`` carries the rules — NOT innerHTML,
  // so the NO-innerHTML invariant holds.  The rules reference the host's
  // ``:root`` CSS variables (``--mono``/``--text``/…), which resolve at paint
  // time regardless of where the <style> node is appended.
  const _AEC_CSS = `
  #aec-view { padding: 14px; overflow: auto; }
  .aec-turn { margin-bottom: 22px; }
  .aec-turn-header {
    display: flex;
    align-items: baseline;
    gap: 10px;
    flex-wrap: wrap;
    margin-bottom: 8px;
  }
  .aec-turn-header .turn-id { font-weight: 600; color: var(--text); }
  .aec-badge {
    font-family: var(--mono);
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 3px;
    border: 1px solid var(--border);
    color: var(--muted);
  }
  .aec-badge.ok { color: var(--accent); border-color: var(--accent); }
  .aec-badge.warn { color: var(--orange); border-color: var(--orange); }
  .aec-track-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 4px 0;
  }
  .aec-track-row .aec-track-label {
    width: 84px;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--muted);
    flex-shrink: 0;
  }
  .aec-track-row canvas {
    border: 1px solid var(--border);
    border-radius: 3px;
    background: #12161e;
  }
  .aec-erle-wrap { margin: 8px 0; }
  .aec-erle-wrap canvas {
    border: 1px solid var(--border);
    border-radius: 3px;
    background: #12161e;
  }
  .aec-section-label {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--muted);
    margin: 8px 0 2px;
  }
  .aec-swimlane {
    display: flex;
    align-items: center;
    gap: 8px;
    margin: 4px 0;
  }
  .aec-swimlane .aec-lane-track {
    position: relative;
    height: 22px;
    border: 1px solid var(--border);
    border-radius: 3px;
    background: #0f1320;
  }
  .aec-fsm-span {
    position: absolute;
    top: 0;
    bottom: 0;
    border-radius: 2px;
    font-size: 9px;
    color: #0b0e14;
    overflow: hidden;
    white-space: nowrap;
    line-height: 22px;
    padding: 0 3px;
  }
  .aec-whatif {
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 8px 0;
    flex-wrap: wrap;
  }
  .aec-whatif input[type="range"] { width: 160px; }
  .aec-whatif .aec-whatif-out {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text);
  }
  `;

  function _injectAecStyles() {
    const doc = global.document;
    if (!doc || doc.getElementById("aec-view-styles")) return;
    const style = doc.createElement("style");
    style.id = "aec-view-styles";
    style.textContent = _AEC_CSS;
    (doc.head || doc.documentElement).appendChild(style);
  }

  // ── AEC view ─────────────────────────────────────────────────────
  // Three aligned audio strips (mic-in / reference / post-AEC), a per-frame
  // ERLE line, double-talk shaded bands, self-echo red ticks, an FSM swimlane
  // built from ``turn_state_changed`` records, and a VAD what-if slider
  // (bundle sources only).  Everything is el()/canvas — NO innerHTML.
  const _AEC_FSM_COLORS = {
    idle: "#3a4150",
    user_speaking: "#44d18a",
    user_paused: "#e8a64b",
    processing: "#b78dff",
    bot_speaking: "#f08fad",
  };

  // One waveform strip backed by /api/audio/waveform; ERLE/self-echo overlays
  // are painted by callers onto a separate mini-canvas.
  function _aecTrackStrip(turnId, label, track, width) {
    const row = el("div", { class: "aec-track-row" });
    row.appendChild(el("div", { class: "aec-track-label" }, label));
    const img = el("img", {
      class: "aec-track-img",
      src: "/api/audio/waveform/" + encodeURIComponent(turnId) +
        "?track=" + encodeURIComponent(track) +
        "&w=" + width + "&h=44",
      alt: label + " waveform",
      width: String(width),
      height: "44",
    });
    row.appendChild(img);
    return row;
  }

  // Paint the per-frame ERLE line plus double-talk shaded bands and self-echo
  // red ticks onto one mini-canvas, all sharing the frame->x mapping.
  function _paintAecErle(canvas, diag) {
    const w = canvas.width, h = canvas.height;
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#12161e";
    ctx.fillRect(0, 0, w, h);
    const erle = (diag.erle && diag.erle.frames) || [];
    const n = Math.max(1, erle.length);
    const xOf = (frame) => (frame / n) * w;
    // Double-talk shaded bands (drawn first, behind the line).
    (diag.double_talk || []).forEach((band) => {
      const x0 = xOf(band.start || 0);
      const x1 = xOf(band.end || 0);
      ctx.fillStyle = "rgba(232,166,75,0.18)";
      ctx.fillRect(x0, 0, Math.max(1, x1 - x0), h);
    });
    // ERLE line, normalised across the measured range (clamped 0..40 dB).
    let lo = 0, hi = 1;
    erle.forEach((v) => {
      if (typeof v !== "number") return;
      if (v < lo) lo = v;
      if (v > hi) hi = v;
    });
    const span = Math.max(1, hi - lo);
    ctx.strokeStyle = "#6ea8fe";
    ctx.beginPath();
    let started = false;
    erle.forEach((v, i) => {
      if (typeof v !== "number") { started = false; return; }
      const x = xOf(i);
      const y = h - ((v - lo) / span) * h;
      if (!started) { ctx.moveTo(x, y); started = true; }
      else ctx.lineTo(x, y);
    });
    ctx.stroke();
    // Self-echo red ticks.
    (diag.self_echo || []).forEach((hit) => {
      const x = xOf(hit.frame || 0);
      ctx.strokeStyle = "#e0635a";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x, h);
      ctx.stroke();
      ctx.lineWidth = 1;
    });
  }

  // Build the turn FSM swimlane from ``turn_state_changed`` records, mapping
  // each contiguous state to a coloured span on the px(ms) timeline.
  function _aecSwimlane(stateRecords, wallMs, width) {
    const wrap = el("div", { class: "aec-swimlane" });
    wrap.appendChild(el("div", { class: "aec-track-label" }, "fsm"));
    const track = el("div", {
      class: "aec-lane-track",
      style: "width: " + width + "px;",
    });
    const span = Math.max(1, wallMs || 1);
    const pxOf = (ms) => Math.max(0, Math.min(width, (ms / span) * width));
    // Order the transitions by mono_ns; derive each span's [start,end) in ms
    // relative to the first transition.
    const ordered = (stateRecords || [])
      .map((r) => ({
        ns: (r.timing && r.timing.mono_ns) || 0,
        to: (r.data && r.data.to) || "idle",
      }))
      .sort((a, b) => a.ns - b.ns);
    if (ordered.length === 0) {
      track.appendChild(el("div", { class: "aec-fsm-span", style:
        "left:0px; width:" + width + "px; background:" + _AEC_FSM_COLORS.idle + ";" }, "idle"));
      wrap.appendChild(track);
      return wrap;
    }
    const base = ordered[0].ns;
    for (let i = 0; i < ordered.length; i++) {
      const startMs = (ordered[i].ns - base) / 1e6;
      const endMs = i + 1 < ordered.length ? (ordered[i + 1].ns - base) / 1e6 : span;
      const x0 = pxOf(startMs);
      const x1 = pxOf(endMs);
      const color = _AEC_FSM_COLORS[ordered[i].to] || _AEC_FSM_COLORS.idle;
      track.appendChild(el("div", {
        class: "aec-fsm-span",
        title: ordered[i].to,
        style: "left:" + x0 + "px; width:" + Math.max(2, x1 - x0) + "px; background:" + color + ";",
      }, ordered[i].to));
    }
    wrap.appendChild(track);
    return wrap;
  }

  function _aecVadWhatif(turnId) {
    const wrap = el("div", { class: "aec-whatif" });
    wrap.appendChild(el("span", { class: "aec-section-label" }, "VAD what-if"));
    const slider = el("input", {
      type: "range", min: "0", max: "100", value: "50", title: "alternate VAD sensitivity",
    });
    const label = el("span", { class: "aec-whatif-out" }, "0.50");
    slider.addEventListener("input", () => {
      label.textContent = (slider.value / 100).toFixed(2);
    });
    const out = el("span", { class: "aec-whatif-out" }, "");
    const recount = el("button", {
      onclick: async () => {
        const threshold = (slider.value / 100).toFixed(2);
        out.textContent = "…";
        try {
          const res = await postJSON(
            "/api/aec/" + encodeURIComponent(turnId) +
              "/vad-whatif?threshold=" + encodeURIComponent(threshold), {});
          const delta = res.false_trigger_delta;
          out.textContent =
            "starts " + res.whatif_starts + " (was " + res.baseline_starts + ", Δ " +
            (delta >= 0 ? "+" : "") + delta + ")";
        } catch (e) {
          out.textContent = "failed: " + e.message;
        }
      },
    }, "Recount");
    wrap.appendChild(slider);
    wrap.appendChild(label);
    wrap.appendChild(recount);
    wrap.appendChild(out);
    return wrap;
  }

  async function renderAecView() {
    const root = $("#aec-view");
    clear(root);
    const turns = state.timeline || [];
    if (turns.length === 0) {
      root.appendChild(el("div", { class: "empty" }, "No turn-scoped records yet."));
      return;
    }
    root.appendChild(el("div", { class: "empty" }, "Loading AEC diagnostics…"));
    const supportsWhatif = state.manifest && state.manifest.source === "bundle";
    const width = 600;
    // Fetch per-turn diagnostics + state-change records concurrently.
    const results = await Promise.all(turns.map(async (turn) => {
      const turnId = turn.turn_id;
      let diag = null, stateRecs = [];
      try {
        diag = await getJSON("/api/aec/" + encodeURIComponent(turnId));
      } catch (e) { diag = { has_reference: false, _error: e.message }; }
      try {
        const r = await getJSON("/api/records?turn=" + encodeURIComponent(turnId) +
          "&name=turn_state_changed&limit=500");
        stateRecs = (r && r.records) || [];
      } catch (e) { stateRecs = []; }
      return { turn, diag, stateRecs };
    }));
    if (state.activeTab && state.activeTab !== "aec") return;
    clear(root);
    for (const { turn, diag, stateRecs } of results) {
      const turnId = turn.turn_id;
      const node = el("div", { class: "aec-turn" });
      const header = el("div", { class: "aec-turn-header" });
      header.appendChild(el("span", { class: "turn-id" }, turnId));
      if (diag && diag.has_reference) {
        header.appendChild(el("span", { class: "aec-badge ok" }, "reference captured"));
        const erle = diag.erle || {};
        if (typeof erle.mean_db === "number") {
          header.appendChild(el("span", { class: "aec-badge" },
            "mean ERLE " + erle.mean_db.toFixed(1) + " dB"));
        }
        const selfEcho = (diag.self_echo || []).length;
        if (selfEcho > 0) {
          header.appendChild(el("span", { class: "aec-badge warn" }, selfEcho + " self-echo"));
        }
      } else {
        header.appendChild(el("span", { class: "aec-badge warn" }, "no AEC reference"));
      }
      node.appendChild(header);

      // Three aligned strips when a reference was captured (mic-in /
      // reference / post-AEC), two otherwise — never claim a strip the
      // journal can't back.  ``has_reference`` mirrors a non-empty
      // ``tracks.reference`` frame count from /api/aec.
      const refFrames =
        (diag && diag.tracks && diag.tracks.reference &&
          diag.tracks.reference.frame_count) || 0;
      node.appendChild(el("div", { class: "aec-section-label" }, "aligned tracks"));
      node.appendChild(_aecTrackStrip(turnId, "mic-in", "mic", width));
      if (refFrames > 0) {
        node.appendChild(_aecTrackStrip(turnId, "reference", "reference", width));
      }
      node.appendChild(_aecTrackStrip(turnId, "post-AEC", "tts", width));

      if (diag && diag.has_reference) {
        node.appendChild(el("div", { class: "aec-section-label" },
          "ERLE (dB) · double-talk shaded · self-echo ticks"));
        const erleWrap = el("div", { class: "aec-erle-wrap" });
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = 60;
        erleWrap.appendChild(canvas);
        node.appendChild(erleWrap);
        _paintAecErle(canvas, diag);
      }

      node.appendChild(el("div", { class: "aec-section-label" }, "turn state machine"));
      node.appendChild(_aecSwimlane(stateRecs, turn.wall_ms, width));

      if (supportsWhatif) node.appendChild(_aecVadWhatif(turnId));
      root.appendChild(node);
    }
  }

  _injectAecStyles();

  global.EasyCatAec = {
    renderAecView: renderAecView,
  };
})(typeof window !== "undefined" ? window : this);
