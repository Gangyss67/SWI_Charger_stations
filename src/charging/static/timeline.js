/* Výběr termínu přímo na časové ose stání (quality of life po C10).
   Myš: stisknout na začátku a táhnout (shift+klik prodlouží). Dotyk / klávesnice: klepnout na začátek,
   pak na konec. Vybírat jde jen souvislý úsek volných slotů v rámci dne, nejvýš MAX_SLOTS.
   Výsledek se zapisuje do polí Začátek a Délka ve formuláři – server kontroluje všechno znovu,
   tohle je jen pohodlnější ovládání. Bez JavaScriptu zůstávají odkazy na slotech a rozbalovací seznamy. */
(function () {
  'use strict';

  // ---- čistá logika (bez DOM), testuje se zvlášť přes node ----
  function rangeOk(free, a, b, max) {
    if (a === null || a === undefined || b < a || b - a + 1 > max) return false;
    for (var i = a; i <= b; i++) if (!free[i]) return false;
    return true;
  }

  // klepnutí: pozdější volný slot v dosahu posune konec, jinak začíná nový výběr
  function tap(sel, i, free, max) {
    if (!free[i]) return sel;
    if (sel && i > sel.start && rangeOk(free, sel.start, i, max)) return { start: sel.start, end: i };
    return { start: i, end: i };
  }

  // tažení: konec se posune jen na místo, kam vede celý volný úsek do limitu
  function drag(sel, i, free, max) {
    if (sel && rangeOk(free, sel.start, i, max)) return { start: sel.start, end: i };
    return sel;
  }

  if (typeof module !== 'undefined') module.exports = { rangeOk: rangeOk, tap: tap, drag: drag };
  if (typeof document === 'undefined') return;

  // ---- napojení na stránku ----
  var timeline = document.querySelector('[data-timeline]');
  var timeSelect = document.getElementById('r-time');
  var slotsSelect = document.getElementById('r-slots');
  var summary = document.getElementById('pick-summary');
  if (!timeline || !timeSelect || !slotsSelect) return;

  var cells = Array.prototype.slice.call(timeline.querySelectorAll('[data-time]'));
  var free = cells.map(function (c) { return c.hasAttribute('data-free'); });
  var max = parseInt(slotsSelect.getAttribute('data-max'), 10) || 8;
  var sel = null, dragging = false, pointerType = null;

  function time(i) { return cells[i].getAttribute('data-time'); }
  function endTime(i) { return i + 1 < cells.length ? time(i + 1) : '24:00'; }
  function duration(n) { return Math.floor(n / 2) + ':' + (n % 2 ? '30' : '00') + ' h'; }

  function render(syncForm) {
    cells.forEach(function (c, i) { c.classList.toggle('selected', !!sel && i >= sel.start && i <= sel.end); });
    if (!sel) return;
    var n = sel.end - sel.start + 1;
    if (syncForm) {
      timeSelect.value = time(sel.start);
      slotsSelect.value = String(n);
    }
    if (summary) {
      summary.textContent = 'Vybráno ' + time(sel.start) + '–' + endTime(sel.end) + ' (' + duration(n) + ').' +
        (n >= max ? ' Delší rezervace není možná.' : ' Táhněte dál nebo klepněte na pozdější slot pro prodloužení.');
    }
  }

  cells.forEach(function (cell, i) {
    if (!free[i]) return;
    cell.setAttribute('draggable', 'false');
    cell.addEventListener('dragstart', function (e) { e.preventDefault(); });
    cell.addEventListener('pointerdown', function (e) {
      pointerType = e.pointerType;
      if (e.pointerType === 'touch' || e.button !== 0) return;
      e.preventDefault();
      sel = e.shiftKey ? tap(sel, i, free, max) : { start: i, end: i };
      dragging = true;
      render(true);
    });
    cell.addEventListener('pointerenter', function () {
      if (!dragging) return;
      sel = drag(sel, i, free, max);
      render(true);
    });
    cell.addEventListener('click', function (e) {
      e.preventDefault(); // bez JS je to odkaz s ?time=, s JS vybíráme na místě
      if (pointerType !== 'mouse' && pointerType !== 'pen') {
        sel = tap(sel, i, free, max);
        render(true);
      }
      pointerType = null;
    });
  });
  document.addEventListener('pointerup', function () { dragging = false; });

  // ruční změna Začátku / Délky ve formuláři -> zvýraznění na ose
  function fromForm(showProblem) {
    var start = -1;
    cells.forEach(function (c, i) { if (time(i) === timeSelect.value) start = i; });
    var n = parseInt(slotsSelect.value, 10) || 1;
    if (start >= 0 && rangeOk(free, start, start + n - 1, max)) {
      sel = { start: start, end: start + n - 1 };
      render(false);
    } else {
      sel = null;
      render(false);
      if (showProblem && summary) summary.textContent = 'Vybraný úsek není na tomhle stání celý volný – zvolte jiný začátek nebo délku.';
    }
  }
  timeSelect.addEventListener('change', function () { fromForm(true); });
  slotsSelect.addEventListener('change', function () { fromForm(true); });
  timeline.classList.add('pickable');
  fromForm(false);
})();
