/* Postgame — HTTP client.
   The single place the frontend talks to the network. Everything else calls
   through js/catalogue.js, so retry policy, timeouts and error shape are
   decided once rather than at every call site.

   Phase 4 of BACKEND-PLAN.md. */
window.PG = window.PG || {};
(function (PG) {
  'use strict';

  var TIMEOUT_MS = 8000;

  /* One retry, and only for a network-level failure. A 4xx is the server
     telling us the request was wrong — repeating it just asks the same bad
     question again, and on a rate-limited endpoint it spends budget that a
     real user needs. A 5xx is not retried either: the server has already done
     the work and failed, and a retry storm is how a struggling backend gets
     pushed over. */
  var RETRY_DELAY_MS = 400;

  /* Same-origin by decision 0.9, so this stays empty in production. Set it
     only to point a local page at a local API on a different port. */
  var base = '';

  function setBase(url) {
    base = url ? String(url).replace(/\/+$/, '') : '';
  }

  /* Every error the caller can see, in one shape. `kind` is what the UI
     branches on; `message` is what a person reads. */
  function ApiError(kind, message, status) {
    this.name = 'ApiError';
    this.kind = kind;          /* 'offline' | 'timeout' | 'http' | 'malformed' */
    this.message = message;
    this.status = status || 0;
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  function url(path, params) {
    /* URLSearchParams rather than string concatenation: it encodes values that
       would otherwise change the meaning of the query, and it drops the whole
       class of bugs where a title containing & silently truncates a filter. */
    var search = new URLSearchParams();
    Object.keys(params || {}).forEach(function (key) {
      var value = params[key];
      if (value === null || value === undefined || value === '') return;
      search.set(key, String(value));
    });
    var qs = search.toString();
    return base + path + (qs ? '?' + qs : '');
  }

  function once(path, params) {
    /* AbortController rather than a Promise.race: race leaves the request
       running, so a slow endpoint keeps a connection open and still resolves
       into nothing. Aborting actually cancels it. */
    var controller = new AbortController();
    var timer = window.setTimeout(function () { controller.abort(); }, TIMEOUT_MS);

    return window.fetch(url(path, params), {
      method: 'GET',
      headers: { 'Accept': 'application/json' },
      credentials: 'same-origin',
      signal: controller.signal
    }).then(function (response) {
      window.clearTimeout(timer);
      return response.json().catch(function () {
        throw new ApiError('malformed', 'The server sent something unreadable.',
          response.status);
      }).then(function (body) {
        if (response.ok) return body;
        /* The server's own message when it wrote one, because those are
           deliberately safe to show; a generic line otherwise. */
        throw new ApiError('http', (body && body.error) ||
          'The server could not answer that.', response.status);
      });
    }, function (err) {
      window.clearTimeout(timer);
      if (err && err.name === 'AbortError') {
        throw new ApiError('timeout', 'That took too long. Check your connection.');
      }
      throw new ApiError('offline', 'Could not reach Postgame. Are you online?');
    });
  }

  function get(path, params) {
    return once(path, params).catch(function (err) {
      /* Retry only what a retry can fix. */
      if (err.kind !== 'offline' && err.kind !== 'timeout') throw err;
      return new Promise(function (resolve) {
        window.setTimeout(resolve, RETRY_DELAY_MS);
      }).then(function () { return once(path, params); });
    });
  }

  /* Is an API reachable at all? Used once at boot to pick a backend, and
     deliberately cheap: /api/health returns {"ok":true} and nothing else. */
  function probe() {
    return once('/api/health').then(function (body) {
      return !!(body && body.ok);
    }, function () { return false; });
  }

  PG.api = {
    get: get,
    probe: probe,
    setBase: setBase,
    ApiError: ApiError,
    timeoutMs: TIMEOUT_MS
  };
})(window.PG);
