var state = {
    timeline: null,
    selectedDay: null,
    dayCache: {},
    allMoments: null,
    mode: 'day',
    calYm: null,
    index: null // timestamp of current moment
  };

  function fetchJSON(url) {
    return fetch(url).then(function(r) { return r.json(); });
  }
  function iso(d) {
    var m = '' + (d.getMonth() + 1), dd = '' + d.getDate();
    if (m.length === 1) m = '0' + m;
    if (dd.length === 1) dd = '0' + dd;
    return d.getFullYear() + '-' + m + '-' + dd;
  }
  function parseDay(s) { return new Date(parseInt(s.slice(0,4)), parseInt(s.slice(5,7))-1, parseInt(s.slice(8,10))); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function loadDay(day, force) {
    if (!force && state.dayCache[day]) return Promise.resolve(state.dayCache[day]);
    return fetchJSON('/api/day/' + day).then(function(d) { state.dayCache[day] = d; return d; });
  }

  function buildAll() {
    if (state.allMoments) return Promise.resolve(state.allMoments);
    var days = (state.timeline.sessions || []).map(function(s) { return s.key; });
    var result = [];
    var chain = Promise.resolve();
    days.forEach(function(day) {
      chain = chain.then(function() {
        return loadDay(day).then(function(d) {
          d.trios.forEach(function(t) { result.push({ day: day, trio: t }); });
        });
      });
    });
    return chain.then(function() { state.allMoments = result; return result; });
  }

  function renderMarkdown(src) {
    var lines = esc(src).split('\n');
    var html = '', inList = false;
    function closeList() { if (inList) { html += '</ul>'; inList = false; } }
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (/^#\s+/.test(line)) { closeList(); html += '<h5 class="mt-2">' + inline(line.replace(/^#\s+/, '')) + '</h5>'; continue; }
      if (/^##\s+/.test(line)) { closeList(); html += '<h5 class="mt-2">' + inline(line.replace(/^##\s+/, '')) + '</h5>'; continue; }
      if (/^###\s+/.test(line)) { closeList(); html += '<h6 class="mt-2">' + inline(line.replace(/^###\s+/, '')) + '</h6>'; continue; }
      if (/^[-*]\s+/.test(line)) { if (!inList) { html += '<ul>'; inList = true; } html += '<li>' + inline(line.replace(/^[-*]\s+/, '')) + '</li>'; continue; }
      closeList();
      if (/^\s*$/.test(line)) { continue; }
      if (/^_{3,}$/.test(line.trim()) || /^\*{3,}$/.test(line.trim())) { html += '<hr>'; continue; }
      html += '<p>' + inline(line) + '</p>';
    }
    closeList();
    return html;
  }
  function inline(t) {
    var s = String(t == null ? '' : t);
    s = s.replace(/\*\*(.+?)\*\*/g, '<strong style="color:#d63384;">$1</strong>');
    s = s.replace(/\*(.+?)\*/g, '<em>$1</em>');
    s = s.replace(/_([^_]+)_/g, '<em>$1</em>');
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    return s;
  }

  function dayLabel(day) {
    var t = parseDay(day);
    var names = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return names[t.getDay()] + ', ' + months[t.getMonth()] + ' ' + t.getDate() + ', ' + t.getFullYear();
  }

  function shortDay(day) {
    var t = parseDay(day);
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return months[t.getMonth()] + ' ' + t.getDate();
  }

  function renderDayTitle() {
    var el = document.getElementById('dayTitle');
    var d = state.dayCache[state.selectedDay];
    if (d && d.session) {
      var s = d.session;
      var sd = s.start_local.slice(0, 10), st = s.start_local.slice(11);
      var ed = s.end_local.slice(0, 10), et = s.end_local.slice(11);
      var range = sd === ed
        ? esc(st) + ' &rarr; ' + esc(et)
        : esc(shortDay(sd)) + ' ' + esc(st) + ' &rarr; ' + esc(shortDay(ed)) + ' ' + esc(et);
      var badge = (state.timeline && state.timeline.gap_hours
        ? ' <span class="badge badge-secondary align-middle">gap &gt; ' + state.timeline.gap_hours + 'h = new day</span>'
        : '');
      el.innerHTML = '<span class="d-inline-block text-left">' +
        '<div class="font-weight-bold">' + esc(dayLabel(sd)) + '</div>' +
        '<div class="text-muted small">' + range + badge + '</div>' +
        '</span>';
    } else {
      el.textContent = dayLabel(state.selectedDay) + ' (' + state.selectedDay + ')';
    }
  }

  function renderSummary(day) {
    var el = document.getElementById('summaryCard');
    var d = state.dayCache[day];
    if (!d || !d.summary) {
      el.innerHTML = '<div class="alert alert-secondary mb-0">No AI summary for ' + esc(day) +
        ' yet &mdash; run <b>bulk_summary.bat</b> to generate it.</div>';
      return;
    }
    el.innerHTML = '<div class="card"><div class="card-header d-flex justify-content-between align-items-center">' +
      '<span class="font-weight-bold">Day Summary &mdash; ' + esc(day) + '</span>' +
      '<span class="text-muted small">' + esc(d.summary.chosen) + '</span></div>' +
      '<div class="card-body" id="summaryBody"></div></div>';
    // The SCHEDULE lives only in the per-hour strip under the slider; keep the
    // static card to DAY OVERVIEW + PATTERNS.
    var rest = d.summary.raw;
    if (/#+\s*SCHEDULE/i.test(rest)) {
      rest = rest.replace(/^[\s\S]*?DAY\s*OVERVIEW/i, 'DAY OVERVIEW');
    }
    // Legacy summaries may still carry the removed COMMENTARY section.
    rest = rest.replace(/^#+\s*COMMENTARY\b[\s\S]*/im, '');
    document.getElementById('summaryBody').innerHTML = renderMarkdown(rest);
  }

  function schedulePanel(day, when) {
    var d = state.dayCache[day];
    if (!d || !d.schedule) return '';
    var s = d.schedule;
    var hour = parseInt((when || '00:00').slice(0, 2), 10) || 0;
    function m(s) { return s.split(':').length === 2 ? parseInt(s.split(':')[0], 10) * 60 + parseInt(s.split(':')[1], 10) : NaN; }
    var hStart = hour * 60, hEnd = hour * 60 + 60;
    var lines = [];
    var blocks = s.hours[('0' + hour).slice(-2)] || [];
    blocks.forEach(function(b) { lines.push('- ' + b.start + ' - ' + b.end + ' | ' + b.text); });
    (s.no_data || []).forEach(function(sp) {
      var s0 = m(sp.start), s1 = m(sp.end);
      if (!isNaN(s0) && !isNaN(s1) && s0 < hEnd && s1 > hStart) {
        lines.push('- [no data ' + sp.start + ' - ' + sp.end + ']');
      }
    });
    if (!lines.length) {
      return '<div class="p-3 bg-light border rounded mt-3 text-muted">No scheduled activity for this hour.</div>';
    }
    return '<div class="mt-3"><div class="font-weight-bold mb-1">Schedule ' +
      ('0' + hour).slice(-2) + ':00' + ' - ' + ('0' + ((hour + 1) % 24)).slice(-2) + ':00</div>' +
      '<div class="p-3 bg-light border rounded trio-desc">' + renderMarkdown(lines.join('\n')) + '</div></div>';
  }

  function rawCaptureDetails(m) {
    var h = '';
    if (m.url) {
      h += '<i class="bi bi-globe2"></i> <a href="' + esc(m.url) + '" target="_blank" rel="noopener">' + esc(m.url) + '</a><br>';
    }
    h += esc(m.label) + '';
    var mtabs = (m.tabs || []).filter(function(tb) { return tb && (tb.title || tb.url); });
    var mwins = (m.windows || []).filter(function(w) { return w && (w.app || w.title); });
    if (mtabs.length) {
      h += '<div class="mb-1">' + (mtabs.length === 1 ? 'Focus tab (this screen):' : 'Focus tabs (this screen):');
      mtabs.forEach(function(tb) {
        h += '<div class="ml-2"><i class="bi bi-globe2"></i> <strong>' + esc(tb.title || '') + '</strong>';
        if (tb.url) h += ' &mdash; <a href="' + esc(tb.url) + '" target="_blank" rel="noopener">' + esc(tb.url) + '</a>';
        h += '</div>';
      });
      h += '</div>';
    }
    if (mwins.length) {
      h += '<details class="small mb-1"><summary class="text-muted">Windows on this screen (' + mwins.length + ')</summary><div class="p-1">';
      mwins.forEach(function(w) {
        h += '<div><span class="text-muted">' + esc(w.app || '') + '</span>' +
              (w.app ? ' &mdash; ' : '') + esc(w.title || '') + '</div>';
      });
      h += '</div></details>';
    }
    return h;
  }

  function renderTrio(entry) {
    var el = document.getElementById('trioView');
    if (!entry) {
      el.innerHTML = '<div class="alert alert-info">Nothing recorded.</div>';
      return;
    }
    var t = entry.trio;
    var v2 = t.format === 'v2';
    var html = '<div class="card mb-3"><div class="card-header d-flex justify-content-between align-items-center"><b>' +
      esc(t.local_time) + '</b>' +
      (t.described ? '<span class="badge badge-success">AI described</span>' : '') +
      '</div><div class="card-body">';

    html += '<div class="row">';
    t.monitors.forEach(function(m) {
      var media;
      if (m.exists) {
        media = '<img src="/screenshots/' + encodeURIComponent(m.file) + '" alt="' + esc(m.label) +
                '" class="img-fluid border mb-1">';
      } else {
        media = '<div class="border text-muted p-3 mb-1 text-center">image pruned</div>';
      }
      html += '<div class="col-lg" style="min-width:320px;">' + media;
      if (v2 && m.ai) {
        html += '<div class="p-2 bg-light border rounded mb-1 trio-desc">' + renderMarkdown(m.ai) + '</div>';
        html += '<details class="small"><summary class="text-muted">Capture details</summary>' +
                '<div class="pt-1 text-muted">' + rawCaptureDetails(m) + '</div></details>';
      } else {
        html += '<div class="text-muted small mb-1">' + rawCaptureDetails(m) + '</div>';
      }
      html += '</div>';
    });
    html += '</div>';

    if (t.described) {
      html += '<div class="mt-2">';
      if (t.tags) {
        var tags = String(t.tags).split(/[,\s]+/).filter(Boolean);
        if (tags.length) {
          html += '<div class="mb-1">';
          tags.forEach(function(tag) { html += '<span class="badge badge-secondary mr-1">' + esc(tag) + '</span>'; });
          html += '</div>';
        }
      }
      if (t.focus) {
        html += '<div class="mb-1"><strong>' + esc(t.focus) + '</strong></div>';
      }
      if (!v2 && t.description) {
        html += '<div class="mb-1 trio-desc">' + renderMarkdown(t.description) + '</div>';
        if (t.secondary) {
          html += '<details class="mt-1"><summary class="text-muted small">Other screens (click to expand)</summary>' +
                  '<div class="p-2 text-muted trio-desc">' + renderMarkdown(t.secondary) + '</div></details>';
        }
      }
      html += '</div>';
    } else {
      html += '<div class="text-muted small mt-2"><i class="bi bi-info-circle"></i> No AI description for this ' +
              'moment yet &mdash; bulk_summary samples about one moment every 20 minutes.</div>';
    }
    // The big text panel is the writer's schedule for this moment's hour.
    html += schedulePanel(entry.day, t.when);
    html += '</div></div>';
    el.innerHTML = html;
  }

  function sliderValue() { return document.getElementById('trioSlider').value; }

  function updateSliderLabel(text) {
    document.getElementById('sliderLabel').textContent = text || '';
  }

  function currentEntry(ts) {
    return nearestEntry(+ts);
  }

  function nearestEntry(targetTs) {
    var timestamps = getTrioTimestamps();
    if (!timestamps.length) return null;
    var bestIdx = 0, bestDiff = Math.abs(timestamps[0] - targetTs);
    for (var i = 1; i < timestamps.length; i++) {
      var diff = Math.abs(timestamps[i] - targetTs);
      if (diff < bestDiff) { bestDiff = diff; bestIdx = i; }
    }
    if (state.mode === 'all') return state.allMoments[bestIdx];
    return { day: state.selectedDay, trio: state.dayCache[state.selectedDay].trios[bestIdx] };
  }

  function getTrioTimestamps() {
    if (state.mode === 'all') {
      return state.allMoments ? state.allMoments.map(function(e) { return e.trio.ts; }) : [];
    }
    var d = state.dayCache[state.selectedDay];
    return d && d.trios ? d.trios.map(function(t) { return t.ts; }) : [];
  }

  function renderSlider() {
    var slider = document.getElementById('trioSlider');
    var timestamps = getTrioTimestamps();
    if (!timestamps.length) {
      slider.min = 0;
      slider.max = 0;
      slider.step = 1;
      slider.value = 0;
      slider.disabled = true;
      paintSlider();
      updateSliderLabel('No moments in this scope.');
      return;
    }
    var minTs = Math.min.apply(null, timestamps);
    var maxTs = Math.max.apply(null, timestamps);
    slider.min = minTs;
    slider.max = maxTs;
    slider.step = 60; // 1 minute steps
    slider.value = Math.min(Math.max(state.index, minTs), maxTs);
    slider.disabled = false;
    paintSlider();
  }

  function renderMain() {
    renderDayTitle();
    renderSlider();
    var ts = +sliderValue();
    var entry = currentEntry(ts);
    renderTrio(entry);
    if (entry) {
      var idx = getTrioTimestamps().indexOf(entry.trio.ts);
      updateSliderLabel(entry.trio.local_time + '   (' + (idx + 1) + ' of ' +
        (state.mode === 'all' ? state.allMoments.length : state.dayCache[state.selectedDay].trios.length) + ')');
    }
    renderSummary(state.mode === 'all' ? (entry ? entry.day : state.selectedDay) : state.selectedDay);
  }

  function blockTimes(blocks) {
    var s = '', i;
    for (i = 0; i < blocks.length; i++) {
      var a = blocks[i][0], b = blocks[i][1];
      if (s) s += ', ';
      s += ('0' + Math.floor(a / 60)).slice(-2) + ':' + ('0' + (a % 60)).slice(-2) +
           '-' + ('0' + Math.floor((b - 1) / 60)).slice(-2) + ':' + ('0' + ((b - 1) % 60)).slice(-2);
    }
    return s;
  }

  function activityBlocksForDay() {
    var d = state.dayCache[state.selectedDay];
    if (!d || !d.trios || !d.trios.length) return null;
    var tsList = d.trios.map(function(t) { return t.ts; }).sort(function(a, b) { return a - b; });
    var base = tsList[0];
    var mins = tsList.map(function(ts) { return Math.floor((ts - base) / 60); });
    var blocks = [], lo = mins[0], hi = mins[0], i;
    for (i = 1; i < mins.length; i++) {
      if (mins[i] - hi <= 15) { hi = mins[i]; }
      else { blocks.push([lo, hi + 1]); lo = hi = mins[i]; }
    }
    blocks.push([lo, hi + 1]);
    return { blocks: blocks, base: base, total: Math.max(1, Math.round((tsList[tsList.length - 1] - base) / 60)) };
  }

  function paintSlider() {
    var slider = document.getElementById('trioSlider');
    if (!slider) return;
    if (state.mode !== 'day') { slider.style.background = ''; return; }
    var info = activityBlocksForDay();
    if (!info) { slider.style.background = ''; return; }
    var minTs = +slider.min;
    var maxTs = +slider.max;
    if (maxTs <= minTs) { slider.style.background = ''; return; }
    var totalSec = maxTs - minTs;
    var segs = 48, stops = [], k;
    for (k = 0; k < segs; k++) {
      var s0 = minTs + totalSec * k / segs;
      var s1 = minTs + totalSec * (k + 1) / segs;
      var on = false, b;
      for (b = 0; b < info.blocks.length; b++) {
        var blockStart = info.base + info.blocks[b][0] * 60;
        var blockEnd = info.base + info.blocks[b][1] * 60;
        if (blockStart < s1 && blockEnd > s0) { on = true; break; }
      }
      var color = on ? '#d63384' : (isDark() ? '#3a3f45' : '#e9ecef');
      stops.push(color + ' ' + (100 * k / segs).toFixed(3) + '%');
      stops.push(color + ' ' + (100 * (k + 1) / segs).toFixed(3) + '%');
    }
    slider.style.background = 'linear-gradient(to right,' + stops.join(',') + ')';
  }

  function renderCalendar() {
    var y = parseInt(state.calYm.slice(0,4)), m = parseInt(state.calYm.slice(5,7));
    var months = ['January','February','March','April','May','June','July','August','September','October','November','December'];
    document.getElementById('calTitle').textContent = months[m-1] + ' ' + y;
    var first = new Date(y, m-1, 1);
    var lead = first.getDay();
    var numDays = new Date(y, m, 0).getDate();
    var dayData = {};
    state.timeline.days.forEach(function(d) { dayData[d.day] = d; });
    var today = state.timeline.today;
    var grid = document.getElementById('calGrid');
    grid.innerHTML = '';
    var i;
    for (i = 0; i < lead; i++) {
      var blank = document.createElement('div');
      blank.className = 'cal-cell cal-blank';
      grid.appendChild(blank);
    }
    for (i = 1; i <= numDays; i++) {
      var dd = (i < 10 ? '0' + i : '' + i);
      var key = state.calYm + '-' + dd;
      var cell = document.createElement('div');
      cell.className = 'cal-cell';
      var has = dayData[key];
      if (has) {
        var a = document.createElement('button');
        a.type = 'button';
        a.className = 'cal-day-btn btn btn-sm ' + (has.has_summary ? 'btn-danger' : 'btn-outline-secondary');
        if (!has.has_summary) a.style.color = '';
        a.textContent = '' + i;
        var hr = has.blocks ? blockTimes(has.blocks) : '';
        a.title = key + (has.count ? ' - ' + has.count + ' moment(s)' : '')
          + (hr ? ' - activity ' + hr : ' - no activity')
          + (has.session && has.session !== key ? ' - part of day ' + has.session : '')
          + (has.has_summary ? ' - AI summary' : '');
        a.addEventListener('click', (function(k) { return function() { selectDay(k); }; })(has.session || key));
        cell.appendChild(a);
        if (has.blocks && has.blocks.length) cell.classList.add('has-activity');
      } else {
        var span = document.createElement('span');
        span.className = 'cal-empty';
        span.textContent = '' + i;
        cell.appendChild(span);
      }
      if (key === today) cell.classList.add('cal-today');
      if (has && has.session === state.selectedDay) cell.classList.add('cal-selected');
      grid.appendChild(cell);
    }
  }

  function selectDay(day) {
    state.selectedDay = day;
    state.mode = 'day';
    updateModeButtons();
    state.index = null;
    loadDay(day).then(function() {
      var d = state.dayCache[day];
      if (d && d.trios.length) state.index = d.trios[d.trios.length - 1].ts;
      renderMain();
      renderCalendar();
      window.scrollTo({ top: 0, behavior: 'smooth' });
    });
  }

  function updateModeButtons() {
    document.getElementById('modeDay').classList.toggle('active', state.mode === 'day');
    document.getElementById('modeAll').classList.toggle('active', state.mode === 'all');
  }

  function setMode(m) {
    if (m === state.mode) return;
    state.mode = m;
    updateModeButtons();
    if (m === 'all' && !state.allMoments) {
      document.getElementById('trioView').innerHTML = '<div class="alert alert-info">Loading all moments...</div>';
      buildAll().then(function() {
        if (state.allMoments && state.allMoments.length) state.index = state.allMoments[state.allMoments.length - 1].trio.ts;
        renderMain();
        renderCalendar();
      });
    } else {
      if (state.mode === 'day') {
        var d = state.dayCache[state.selectedDay];
        if (d && d.trios.length) state.index = d.trios[d.trios.length - 1].ts;
      } else if (state.allMoments && state.allMoments.length) {
        state.index = state.allMoments[state.allMoments.length - 1].trio.ts;
      }
      renderMain();
      renderCalendar();
    }
  }

  function describedIndices() {
    var trios = state.dayCache[state.selectedDay] ? state.dayCache[state.selectedDay].trios : [];
    var idx = [];
    trios.forEach(function(t, i) { if (t.described) idx.push(i); });
    return idx;
  }

  function onSliderInput() {
    var ts = +sliderValue();
    var entry = currentEntry(ts);
    renderTrio(entry);
    if (entry) {
      var timestamps = getTrioTimestamps();
      var idx = timestamps.indexOf(entry.trio.ts);
      updateSliderLabel(entry.trio.local_time + '   (' + (idx + 1) + ' of ' +
        (state.mode === 'all' ? state.allMoments.length : state.dayCache[state.selectedDay].trios.length) + ')');
      if (state.mode === 'all' && entry.day !== state.selectedDay) {
        state.selectedDay = entry.day;
        renderDayTitle();
        renderSummary(entry.day);
        renderCalendar();
      }
    }
  }

  function jumpDescribed(dir) {
    var describedTs = describedTimestamps();
    if (!describedTs.length) return;
    var cur = +sliderValue();
    var target = describedTs[0];
    if (dir > 0) {
      for (var i = 0; i < describedTs.length; i++) {
        if (describedTs[i] > cur) { target = describedTs[i]; break; }
      }
    } else {
      for (var i = describedTs.length - 1; i >= 0; i--) {
        if (describedTs[i] < cur) { target = describedTs[i]; break; }
      }
    }
    document.getElementById('trioSlider').value = target;
    onSliderInput();
  }

  function describedTimestamps() {
    var idx = describedIndices();
    var timestamps = getTrioTimestamps();
    return idx.map(function(i) { return timestamps[i]; }).filter(function(ts) { return ts != null; });
  }

  function isDark() { return document.body.classList.contains('theme-dark'); }

  function applyTheme(theme) {
    document.body.classList.toggle('theme-dark', theme === 'dark');
    var btn = document.getElementById('darkToggle');
    if (btn) {
      var ic = btn.querySelector('i');
      if (ic) ic.className = theme === 'dark' ? 'bi bi-sun' : 'bi bi-moon';
    }
    if (typeof paintSlider === 'function') paintSlider();
  }

  function initTheme() {
    var saved = null;
    try { saved = localStorage.getItem('or.theme'); } catch (e) {}
    // Dark is the default; the OS preference is only consulted when the user
    // has explicitly asked for "auto" (stored as 'auto' by the toggle).
    var theme = saved || 'dark';
    if (theme === 'auto') {
      theme = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
    }
    applyTheme(theme);
    var btn = document.getElementById('darkToggle');
    if (btn) {
      btn.addEventListener('click', function() {
        var next = isDark() ? 'light' : 'dark';
        applyTheme(next);
        try { localStorage.setItem('or.theme', next); } catch (e) {}
      });
    }
  }

  function init() {
    document.getElementById('trioSlider').addEventListener('input', onSliderInput);
    document.getElementById('prevDescribed').addEventListener('click', function() { jumpDescribed(-1); });
    document.getElementById('nextDescribed').addEventListener('click', function() { jumpDescribed(1); });

    document.getElementById('modeDay').addEventListener('click', function() { setMode('day'); });
    document.getElementById('modeAll').addEventListener('click', function() { setMode('all'); });

    document.getElementById('prevDay').addEventListener('click', function() {
      var sessKeys = (state.timeline.sessions || []).map(function(s) { return s.key; });
      var idx = sessKeys.indexOf(state.selectedDay);
      if (idx > 0) { selectDay(sessKeys[idx - 1]); return; }
      var d = parseDay(state.selectedDay); d.setDate(d.getDate() - 1); selectDay(iso(d));
    });
    document.getElementById('nextDay').addEventListener('click', function() {
      var sessKeys = (state.timeline.sessions || []).map(function(s) { return s.key; });
      var idx = sessKeys.indexOf(state.selectedDay);
      if (idx >= 0 && idx < sessKeys.length - 1) { selectDay(sessKeys[idx + 1]); return; }
      var d = parseDay(state.selectedDay); d.setDate(d.getDate() + 1); selectDay(iso(d));
    });
    document.getElementById('calPrev').addEventListener('click', function() {
      var d = new Date(parseInt(state.calYm.slice(0,4)), parseInt(state.calYm.slice(5,7)) - 2, 1);
      state.calYm = iso(d).slice(0,7); renderCalendar();
    });
    document.getElementById('calNext').addEventListener('click', function() {
      var d = new Date(parseInt(state.calYm.slice(0,4)), parseInt(state.calYm.slice(5,7)), 1);
      state.calYm = iso(d).slice(0,7); renderCalendar();
    });

    // Deep link from an "Ask" result: /?day=<session key>&ts=<timestamp>.
    var params = new URLSearchParams(window.location.search);
    var wantedTs = parseInt(params.get('ts') || '', 10);

    fetchJSON('/api/timeline').then(function(t) {
      state.timeline = t;
      var wanted = params.get('day');
      var sessKeys = (t.sessions || []).map(function(s) { return s.key; });
      state.selectedDay = (wanted && sessKeys.indexOf(wanted) >= 0)
        ? wanted
        : (sessKeys.length ? sessKeys[sessKeys.length - 1] : t.today);
      state.calYm = state.selectedDay.slice(0,7);
      return loadDay(state.selectedDay);
    }).then(function() {
      var d = state.dayCache[state.selectedDay];
      if (d && d.trios.length) {
        state.index = d.trios[d.trios.length - 1].ts;
        if (!isNaN(wantedTs)) {
          // A linked moment may have been pruned since the link was made, so
          // accept anything near the surviving range and let the slider snap.
          var lo = d.trios[0].ts, hi = d.trios[d.trios.length - 1].ts;
          if (wantedTs >= lo - 6 * 3600 && wantedTs <= hi + 6 * 3600) {
            state.index = wantedTs;
            showJumpNote(wantedTs);
          }
        }
      }
      renderMain();
      renderCalendar();
    });
  }

  function showJumpNote(ts) {
    var wrap = document.getElementById('jumpNote');
    if (!wrap) return;
    var when = new Date(ts * 1000);
    var days = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
    var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    var label = days[when.getDay()] + ' ' + when.getDate() + ' ' + months[when.getMonth()] +
                ' ' + when.getFullYear() + ', ' +
                ('0' + when.getHours()).slice(-2) + ':' + when.getMinutes();
    var box = document.getElementById('jumpNoteBox');
    if (box) box.textContent = label;
    wrap.classList.remove('d-none');
  }

  document.addEventListener('DOMContentLoaded', function() {
    initTheme();
    if (document.getElementById('trioSlider')) init();
  });