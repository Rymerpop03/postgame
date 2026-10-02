/* Postgame — appearance
   Three preferences: follow the system, force light, force dark. Only the
   *resolved* value ever reaches the document as data-theme, so the stylesheet
   needs one light block rather than one per preference.

   A small copy of resolve() runs inline in <head> so the first paint is already
   the right colour; without it a light-theme reader gets a dark flash on every
   page load. Keep the two in step. */
window.PG = window.PG || {};
(function (PG) {
  'use strict';

  var KEY = 'postgame.theme';
  var MODES = ['system', 'light', 'dark'];
  var LABELS = { system: 'System', light: 'Light', dark: 'Dark' };
  /* Matches --ink-900 in each theme, so the browser chrome agrees with the page. */
  var BAR = { dark: '#0b0e16', light: '#f4f6fa' };

  var query = window.matchMedia ?
    window.matchMedia('(prefers-color-scheme: light)') : null;

  function read() {
    try {
      var saved = window.localStorage.getItem(KEY);
      return MODES.indexOf(saved) > -1 ? saved : 'system';
    } catch (e) {
      return 'system';                  /* private mode, or storage disabled */
    }
  }

  function resolve(mode) {
    if (mode === 'light' || mode === 'dark') return mode;
    return query && query.matches ? 'light' : 'dark';
  }

  function apply(mode) {
    var actual = resolve(mode);
    document.documentElement.setAttribute('data-theme', actual);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute('content', BAR[actual]);
    return actual;
  }

  function set(mode) {
    if (MODES.indexOf(mode) === -1) mode = 'system';
    try { window.localStorage.setItem(KEY, mode); } catch (e) { /* not fatal */ }
    return apply(mode);
  }

  /* While the preference is "system", track the OS switching under us. */
  if (query) {
    var onChange = function () { if (read() === 'system') apply('system'); };
    if (query.addEventListener) query.addEventListener('change', onChange);
    else if (query.addListener) query.addListener(onChange);
  }

  apply(read());

  PG.theme = {
    modes: MODES,
    labels: LABELS,
    get: read,
    set: set,
    resolved: function () { return resolve(read()); }
  };
})(window.PG);
