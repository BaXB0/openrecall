(function () {
  var wasAtBottom = true;
  var lastText = '';

  function el(id) { return document.getElementById(id); }

  function tick() {
    fetch('/api/ai-status?lines=300').then(function (r) { return r.json(); }).then(function (st) {
      el('aiStage').textContent = (st.running ? 'Running: ' : 'Summary: ') + (st.stage_label || st.stage);
      var dayBits = [];
      if (st.day) dayBits.push('Day: ' + st.day);
      if (st.pending_day) dayBits.push('Pending: ' + st.pending_day);
      el('aiDay').textContent = dayBits.join('   \u00b7   ');

      var bar = el('aiBar');
      var barBase = 'progress-bar progress-bar-striped';
      if (st.running && st.stage === 'vision' && st.total) {
        bar.className = barBase + ' progress-bar-animated';
        bar.style.width = Math.round(100 * st.cur / st.total) + '%';
        el('aiScene').textContent = (st.minute ? st.minute + ' ' : '') +
          'Sampled ' + st.cur + '/' + st.total + (st.scene ? ' \u2014 ' + st.scene : '');
      } else if (st.running && st.stage === 'writer' && st.hour_now) {
        bar.className = barBase + ' progress-bar-animated';
        bar.style.width = '100%';
        el('aiScene').textContent = 'Writing hour ' + st.hour_now + ':00' + (st.scene ? ' \u2014 ' + st.scene : '');
      } else {
        bar.className = barBase + (st.running ? ' progress-bar-animated' : '');
        bar.style.width = st.running ? '100%' : '0%';
        el('aiScene').textContent = st.scene || (st.running ? 'Working\u2026' : '');
      }

      el('aiDone').textContent = (st.days_done && st.days_done.length)
        ? ('Summaries done: ' + st.days_done.length + (st.latest_done ? ' (latest ' + st.latest_done + ')' : ''))
        : '';

      var log = el('aiLog');
      var text = (st.log_tail || []).join('\n');
      if (text !== lastText) {
        lastText = text;
        log.textContent = text;
      }
      el('aiUpdated').textContent = 'updated ' + new Date().toLocaleTimeString();
      if (wasAtBottom) log.scrollTop = log.scrollHeight;
    }).catch(function () {});
  }

  document.addEventListener('DOMContentLoaded', function () {
    var pre = el('aiLog');
    if (pre) {
      pre.addEventListener('scroll', function () {
        wasAtBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
      });
    }
    tick();
    setInterval(tick, 2000);
  });
})();
