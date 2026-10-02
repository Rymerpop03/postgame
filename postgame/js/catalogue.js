/* Postgame — the catalogue seam.
   One async interface with two implementations behind it, chosen once at boot.

   Decision 0.2 in BACKEND-PLAN.md: the site keeps working from file://, where
   there is no origin and therefore no API. So:

     LocalCatalogue  reads the 2,000 games out of js/data.js, exactly as the
                     site did before Phase 4. Used on file://, and as the
                     fallback when the API cannot be reached.
     ApiCatalogue    reads them from Postgres over HTTP.

   Both return promises. Callers cannot tell which one answered, which is the
   point — the alternative is every view builder knowing about deployment.

   Scope note: this covers the *catalogue* only. Members, logs, reviews and
   messages still come from js/store.js against localStorage, because the
   endpoints that serve them do not exist until Phases 7 to 10. That split is
   visible in exactly one place: resolve() near the bottom. */
window.PG = window.PG || {};
(function (PG) {
  'use strict';

  var PAGE_SIZE = 24;
  /* The count the home page has always shown. */
  var TRENDING_COUNT = 14;

  /* The values the filter bar already uses. Kept here so the query-string
     vocabulary the UI has always had survives the move to HTTP. */
  var SORTS = {
    popular: 1, rating: 1, logged: 1, critic: 1,
    newest: 1, oldest: 1, title: 1
  };

  function sortName(value) {
    return SORTS[value] ? value : 'popular';
  }

  /* -------------------------------------------------------------- local */

  /* Reproduces what app.js did inline before Phase 4, so the two backends
     genuinely agree. Not new logic — the old logic behind a promise. */
  function LocalCatalogue() {
    function match(game, q) {
      if (!q) return true;
      var needle = q.toLowerCase();
      return game.title.toLowerCase().indexOf(needle) !== -1 ||
        (game.developer || '').toLowerCase().indexOf(needle) !== -1;
    }

    function compare(sort) {
      if (sort === 'title') {
        return function (a, b) { return a.title.localeCompare(b.title); };
      }
      if (sort === 'newest') {
        return function (a, b) { return (b.year || 0) - (a.year || 0); };
      }
      if (sort === 'oldest') {
        return function (a, b) { return (a.year || 9999) - (b.year || 9999); };
      }
      if (sort === 'critic') {
        return function (a, b) { return (b.critic || -1) - (a.critic || -1); };
      }
      if (sort === 'rating') {
        return function (a, b) {
          return (PG.store.ratingSummary(b.id).average || -1) -
            (PG.store.ratingSummary(a.id).average || -1);
        };
      }
      if (sort === 'logged') {
        return function (a, b) {
          return PG.store.ratingSummary(b.id).count -
            PG.store.ratingSummary(a.id).count;
        };
      }
      return function (a, b) { return a.rank - b.rank; };
    }

    this.list = function (query) {
      var q = query || {};
      var list = PG.data.games.filter(function (game) {
        if (!match(game, q.q)) return false;
        if (q.genre && game.genres.indexOf(q.genre) === -1) return false;
        if (q.platform && game.platforms.indexOf(q.platform) === -1) return false;
        if (q.decade) {
          var decade = Math.floor((game.year || 0) / 10) * 10;
          if (String(decade) !== String(q.decade)) return false;
        }
        if (q.year && String(game.year) !== String(q.year)) return false;
        return true;
      }).slice().sort(compare(sortName(q.sort)));

      var page = Math.max(1, q.page || 1);
      return Promise.resolve({
        games: list.slice(0, PAGE_SIZE * page),
        total: list.length,
        hasMore: list.length > PAGE_SIZE * page
      });
    };

    this.get = function (id) {
      return Promise.resolve(PG.data.byId[id] || null);
    };

    this.facets = function () {
      return Promise.resolve({
        genres: PG.data.genres.map(function (g) {
          return { name: g.name, count: g.count };
        }),
        platforms: PG.data.platforms.map(function (p) {
          return { name: p.name, count: p.count };
        }),
        decades: PG.data.decades,
        total: PG.data.games.length
      });
    };

    this.suggest = function (q) {
      return Promise.resolve(PG.data.search(q, 8));
    };

    this.trending = function () {
      /* data.trending is a *function*, not an array — resolving with the
         function itself sailed past the API backend (which has its own
         implementation) and broke only the local one. */
      return Promise.resolve(PG.data.trending(TRENDING_COUNT));
    };

    this.name = 'local';
  }

  /* ---------------------------------------------------------------- api */

  function ApiCatalogue() {
    /* A row from the API is shaped for the client; the rest of the frontend
       expects the shape js/data.js produced. Translating here means no view
       builder needs to know which backend answered. `developer` is `studio`
       server-side, and renaming it in the UI instead would touch seven files. */
    function adapt(row) {
      if (!row) return null;
      return {
        id: row.id,
        title: row.title,
        developer: row.studio,
        year: row.year,
        genres: row.genres || [],
        platforms: row.platforms || [],
        critic: row.critic,
        blurb: row.blurb || '',
        steam: row.steamAppId || null,
        hue: row.hue,
        pattern: row.pattern,
        rank: row.rank,
        /* Community numbers from game_stats. Zero until Phase 7 gives people a
           way to write logs through the API. */
        apiRatingAverage: row.ratingAverage,
        apiRatingCount: row.ratingCount,
        apiLogCount: row.logCount
      };
    }

    /* The grid pages by clicking "show more", which the old code did by slicing
       a longer prefix each time. Keyset cursors do not work that way, so the
       cursor for each page is remembered against the filters it belongs to.
       Changing any filter produces a different key and starts again. */
    var cursors = {};

    function keyFor(q) {
      return [q.q || '', q.genre || '', q.platform || '', q.decade || '',
        q.year || '', sortName(q.sort)].join('');
    }

    this.list = function (query) {
      var q = query || {};
      var page = Math.max(1, q.page || 1);
      var known = cursors[keyFor(q)] || (cursors[keyFor(q)] = [null]);

      function params(cursor) {
        var out = {
          q: q.q, genre: q.genre, platform: q.platform, year: q.year,
          sort: sortName(q.sort), limit: PAGE_SIZE
        };
        if (cursor) out.cursor = cursor;
        return out;
      }

      /* The API cannot jump to page N, so walk forward, accumulating. In
         practice `page` only ever grows by one, because changing a filter
         resets it — the walk is one request in the normal case. */
      function step(index, collected) {
        return PG.api.get('/api/games', params(known[index])).then(function (body) {
          var all = collected.concat((body.games || []).map(adapt));
          if (body.nextCursor && known.length === index + 1) {
            known.push(body.nextCursor);
          }
          if (index + 1 < page && body.nextCursor) return step(index + 1, all);
          return { games: all, total: body.total, hasMore: !!body.nextCursor };
        });
      }

      return step(0, []);
    };

    this.get = function (id) {
      return PG.api.get('/api/games/' + encodeURIComponent(id))
        .then(adapt, function (err) {
          if (err && err.status === 404) return null;
          throw err;
        });
    };

    this.facets = function () {
      return PG.api.get('/api/catalogue/facets').then(function (body) {
        /* The API reports years; the filter bar offers decades. Folding here
           keeps the UI's vocabulary unchanged. */
        var counts = {};
        (body.years || []).forEach(function (year) {
          var decade = Math.floor(year / 10) * 10;
          counts[decade] = (counts[decade] || 0) + 1;
        });
        var decades = Object.keys(counts).sort(function (a, b) {
          return Number(b) - Number(a);
        }).map(function (d) {
          return { decade: Number(d), count: counts[d] };
        });
        return {
          genres: (body.genres || []).map(function (g) {
            return { name: g.name, count: g.game_count };
          }),
          platforms: (body.platforms || []).map(function (p) {
            return { name: p.name, count: p.game_count };
          }),
          decades: decades,
          total: body.total
        };
      });
    };

    this.suggest = function (q) {
      return PG.api.get('/api/search/suggest', { q: q }).then(function (body) {
        return (body.results || []).map(function (row) {
          return {
            id: row.id, title: row.title, year: row.year,
            developer: row.studio, hue: row.hue, pattern: row.pattern
          };
        });
      });
    };

    /* No trending endpoint yet: trending is a slice of the authored order,
       which is exactly what the first page of the popular sort already is. */
    this.trending = function () {
      return this.list({ sort: 'popular', page: 1 }).then(function (page) {
        return page.games.slice(0, TRENDING_COUNT);
      });
    };

    this.name = 'api';
  }

  /* --------------------------------------------------------------- boot */

  var active = null;
  var ready = null;

  /* Resolving a game for a *seeded* log, review or shelf. Those rows live in
     localStorage and reference games by the same slug, so the local index
     answers them without a request — and synchronously, which is what lets
     Phase 4 convert the catalogue without also rewriting every social view.
     Phases 7 to 10 move those rows to the API and this disappears. */
  function resolve(id) {
    return PG.data.byId[id] || null;
  }

  function choose() {
    if (ready) return ready;

    /* file:// has an opaque origin: no cookies, no same-origin fetch, nothing
       for an API to be. Do not even probe — a failed request there is console
       noise, not information. */
    if (window.location.protocol === 'file:') {
      active = new LocalCatalogue();
      ready = Promise.resolve(active);
      return ready;
    }

    ready = PG.api.probe().then(function (reachable) {
      active = reachable ? new ApiCatalogue() : new LocalCatalogue();
      return active;
    });
    return ready;
  }

  function call(method, args) {
    return choose().then(function (backend) {
      return backend[method].apply(backend, args);
    });
  }

  PG.catalogue = {
    list: function (q) { return call('list', [q]); },
    get: function (id) { return call('get', [id]); },
    facets: function () { return call('facets', []); },
    suggest: function (q) { return call('suggest', [q]); },
    trending: function () { return call('trending', []); },
    resolve: resolve,
    ready: choose,
    pageSize: PAGE_SIZE,
    /* For the boot log and the tests: which backend answered. */
    backend: function () { return active ? active.name : 'undecided'; }
  };
})(window.PG);
