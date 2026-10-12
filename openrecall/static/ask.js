(function() {
  'use strict';

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  var form = document.getElementById('askForm');
  var input = document.getElementById('q');
  var btn = document.getElementById('askBtn');
  var useAi = document.getElementById('useAi');
  var status = document.getElementById('askStatus');
  var error = document.getElementById('askError');
  var result = document.getElementById('askResult');

  function setBusy(busy, msg) {
    btn.disabled = busy;
    status.classList.toggle('d-none', !busy);
    if (busy && msg) status.textContent = msg;
  }

  function showError(msg) {
    error.textContent = msg;
    error.classList.remove('d-none');
  }

  function badge(text, cls) {
    return el('span', 'badge ' + (cls || 'badge-secondary') + ' mr-1', text);
  }

  function renderInterpreted(data) {
    var p = data.parsed || {};
    var bits = [];
    if (p.intent) bits.push('looking for the ' + p.intent);
    if (p.window_label) bits.push('within ' + p.window_label);
    if (p.terms && p.terms.length) bits.push('about ' + p.terms.join(', '));
    if (p.daypart) bits.push(p.daypart[0] + ' only');
    var row = el('div', 'small text-muted');
    row.appendChild(el('i', 'bi bi-signpost-2 mr-1'));
    row.appendChild(document.createTextNode(bits.join(' \u00b7 ') || 'no filters'));
    return row;
  }

  function renderHit(h, isBest) {
    var a = el('a', 'list-group-item list-group-item-action');
    a.href = h.link;

    var top = el('div', 'd-flex justify-content-between align-items-start');
    var left = el('div');
    left.appendChild(el('div', 'font-weight-bold', h.span));
    if (h.moments > 1) left.appendChild(badge(h.moments + ' moments', 'badge-info'));
    if (isBest) left.appendChild(badge('best match', 'badge-success'));
    if (h.kind === 'day') left.appendChild(badge('day summary', 'badge-secondary'));
    top.appendChild(left);
    top.appendChild(el('span', 'text-muted small ml-2', h.age));
    a.appendChild(top);

    if (h.snippet) a.appendChild(el('div', 'small text-muted mt-1', h.snippet));

    var open = el('div', 'small mt-1');
    open.appendChild(el('i', 'bi bi-clock-history mr-1'));
    open.appendChild(document.createTextNode('open in timeline \u2192'));
    a.appendChild(open);
    return a;
  }

  function render(data) {
    result.innerHTML = '';
    error.classList.add('d-none');

    if (!data.hits || !data.hits.length) {
      var empty = el('div', 'alert alert-secondary mb-0', data.answer || 'Nothing found.');
      result.appendChild(empty);
      return;
    }

    // Answer
    var card = el('div', 'card mb-3');
    var head = el('div', 'card-header d-flex justify-content-between align-items-center');
    head.appendChild(el('span', 'font-weight-bold', 'Answer'));
    var right = el('span', 'small');
    right.appendChild(badge(data.used_ai ? 'model-ranked' : 'keyword-ranked',
                            data.used_ai ? 'badge-success' : 'badge-secondary'));
    right.appendChild(el('span', 'text-muted',
                         data.indexed + ' moments \u00b7 ' + data.days + ' days'));
    head.appendChild(right);
    card.appendChild(head);

    var body = el('div', 'card-body');
    body.appendChild(el('p', 'mb-2', data.answer || ''));
    body.appendChild(renderInterpreted(data));
    if (data.note) {
      var note = el('div', 'small text-muted mt-2 mb-0');
      note.appendChild(el('i', 'bi bi-info-circle mr-1'));
      note.appendChild(document.createTextNode(data.note));
      body.appendChild(note);
    }
    if (data.window_fallback) {
      body.appendChild(el('div', 'small text-muted mt-1 mb-0',
        'Nothing was found inside that window, so the closest matches from all time are shown.'));
    }
    card.appendChild(body);
    result.appendChild(card);

    // Hits
    var hits = el('div', 'card');
    var hh = el('div', 'card-header d-flex justify-content-between align-items-center');
    hh.appendChild(el('span', 'font-weight-bold', 'Matches'));
    hh.appendChild(el('span', 'text-muted small', data.hits.length + ' shown'));
    hits.appendChild(hh);
    var list = el('div', 'list-group list-group-flush');
    data.hits.forEach(function(h, i) { list.appendChild(renderHit(h, i === 0)); });
    hits.appendChild(list);
    result.appendChild(hits);
  }

  function run(q) {
    if (!q) return;
    input.value = q;
    error.classList.add('d-none');
    // The model pass can take a while on a cold model (it has to be paged in).
    setBusy(true, useAi.checked
      ? 'Thinking\u2026 the first question of a session may take ~15s while the local model loads.'
      : 'Searching\u2026');
    result.innerHTML = '';

    var url = '/api/ask?q=' + encodeURIComponent(q) + (useAi.checked ? '' : '&ai=0');
    fetch(url)
      .then(function(r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function(data) {
        render(data);
        try {
          history.replaceState(null, '', '/ask?q=' + encodeURIComponent(q));
        } catch (e) {}
      })
      .catch(function(e) { showError('Request failed: ' + e.message); })
      .then(function() { setBusy(false); });
  }

  form.addEventListener('submit', function(e) {
    e.preventDefault();
    run(input.value.trim());
  });

  Array.prototype.forEach.call(document.querySelectorAll('.ask-example'), function(a) {
    a.addEventListener('click', function(e) {
      e.preventDefault();
      run(a.getAttribute('data-q'));
    });
  });

  document.addEventListener('DOMContentLoaded', function() {
    var q = new URLSearchParams(window.location.search).get('q');
    if (q) run(q);
  });
})();
