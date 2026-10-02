/* Postgame — router, views and interactions
   Hash routing keeps the whole site working from the filesystem: no server, no
   build step, no fetch calls. */
(function (PG) {
  'use strict';

  var ui = PG.ui;
  var data = PG.data;
  var store = PG.store;

  var PAGE_SIZE = 48;
  var FEED_PAGE = 20;

  var view;
  var state = {
    gamesPage: 1, feedPage: 1, authTab: 'in', authError: '', settingsError: '',
    threads: {}
  };

  /* ===================================================== routing utilities */

  function parseHash() {
    var raw = window.location.hash.replace(/^#\/?/, '');
    var cut = raw.indexOf('?');
    var path = cut > -1 ? raw.slice(0, cut) : raw;
    var query = {};
    if (cut > -1) {
      raw.slice(cut + 1).split('&').forEach(function (pair) {
        if (!pair) return;
        var kv = pair.split('=');
        try {
          query[decodeURIComponent(kv[0])] = decodeURIComponent((kv[1] || '').replace(/\+/g, ' '));
        } catch (e) { /* ignore malformed */ }
      });
    }
    return {
      parts: path.split('/').filter(Boolean).map(function (p) {
        try { return decodeURIComponent(p); } catch (e) { return p; }
      }),
      query: query
    };
  }

  function toQuery(obj) {
    var out = [];
    Object.keys(obj).forEach(function (k) {
      var v = obj[k];
      if (v === '' || v === undefined || v === null) return;
      out.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
    });
    return out.length ? '?' + out.join('&') : '';
  }

  function go(hash) { window.location.hash = hash; }

  /* ===================================================== shared components */

  function sectionHead(title, linkText, linkHref) {
    return '<div class="section__head"><h2 class="section__title">' + ui.esc(title) +
      '</h2>' + (linkText ? '<a class="section__link" href="' + linkHref + '">' +
      ui.esc(linkText) + ' →</a>' : '') + '</div>';
  }

  function recentLogs(limit) {
    return store.allLogs()
      .filter(function (l) { return l.rating; })
      .sort(function (a, b) { return (b.date || '').localeCompare(a.date || ''); })
      .slice(0, limit);
  }

  function popularReviews(limit) {
    return store.allLogs()
      .filter(function (l) { return l.review; })
      .sort(function (a, b) { return store.reviewLikes(b) - store.reviewLikes(a); })
      .slice(0, limit);
  }

  function summaryOf(game) { return store.ratingSummary(game.id); }

  /* Which review threads are expanded. This is view state, not stored data, but
     it still has to survive the full re-render that every action triggers — so
     it lives in `state` and every ui.review call reads it from here. */
  function reviewOpts(log, extra) {
    var opts = extra || {};
    opts.openThread = !!state.threads[log.id];
    return opts;
  }

  /* Review ids are generated, not typed, but they still end up inside attribute
     selectors — quote them properly rather than trusting the format. */
  function cssQuote(value) {
    if (window.CSS && window.CSS.escape) return window.CSS.escape(value);
    return String(value).replace(/["\\]/g, '\\$&');
  }

  function focusToggle(logId) {
    var button = document.querySelector(
      '[data-act="toggle-thread"][data-id="' + cssQuote(logId) + '"]');
    if (button) button.focus();
  }

  function paintCommentCount(field) {
    var form = field.closest('.cform');
    var count = form && form.querySelector('.charcount');
    if (count) count.textContent = field.value.length + ' / ' + store.commentMax;
  }

  /* A chat log is read from the bottom. The scroller is its own element rather
     than the page, so this has to be set explicitly after every render. */
  function scrollThreadToEnd() {
    var box = document.querySelector('.msgs');
    if (box) box.scrollTop = box.scrollHeight;
  }

  /* ================================================================== home */

  /* Home mixes two sources on purpose, and it is worth being clear about it.
     The featured game and the trending strip are *catalogue* data, so they come
     through the seam. "Highest rated" and the feeds are derived from the seeded
     community — ratings, logs, follows — which has no endpoints until Phases 7
     to 10, so it stays on localStorage. Phase 7 moves the second half over. */
  function viewHome() {
    return Promise.all([
      PG.catalogue.list({ sort: 'popular', page: 1 }),
      PG.catalogue.trending()
    ]).then(function (both) {
      return renderHome(both[0].games, both[1]);
    });
  }

  function renderHome(top, trending) {
    var me = store.currentUser();
    /* Deterministic per day, and drawn from the first page rather than the
       whole catalogue so it does not need every game in memory. */
    var featured = top[PG.util.hash32(new Date().toISOString().slice(0, 10)) % top.length];
    var fsum = summaryOf(featured);

    var hero;
    if (me) {
      var stats = store.memberStats(me.username);
      hero = '<section class="hero" style="--h:' + ui.hue(featured.hue) + '">' +
        '<a class="hero__poster" href="#/game/' + ui.attr(featured.id) + '">' +
          ui.cover(featured) + '</a>' +
        '<div>' +
          '<div class="hero__eyebrow">Featured today</div>' +
          '<h1 class="hero__title">' + ui.esc(featured.title) + '</h1>' +
          '<div class="hero__meta">' + ui.esc(featured.year) + ' · ' +
            ui.esc(featured.developer) + ' · ' + ui.stars(fsum.average) +
            ' <b>' + ui.fmtRating(fsum.average) + '</b></div>' +
          '<p class="hero__blurb">' + ui.esc(featured.blurb) + '</p>' +
          '<div class="hero__actions">' +
            '<button class="btn btn--primary" data-act="log" data-game="' +
              ui.attr(featured.id) + '">' + ui.icon('plus') + 'Log this game</button>' +
            '<a class="btn" href="#/member/' + ui.attr(me.username) + '">Your diary · ' +
              stats.logged + ' logged</a>' +
          '</div>' +
        '</div>' +
      '</section>';
    } else {
      hero = '<section class="hero" style="--h:' + ui.hue(featured.hue) + '">' +
        '<a class="hero__poster" href="#/game/' + ui.attr(featured.id) + '">' +
          ui.cover(featured) + '</a>' +
        '<div>' +
          '<div class="hero__eyebrow">Your games, remembered</div>' +
          '<h1 class="hero__title">Log the games you play.</h1>' +
          '<p class="hero__blurb">Keep a diary of everything you finish, rate it out of five ' +
            'stars, write the review you would have wanted to read, and see what ' +
            'everyone else made of it. ' + ui.fmtNumber(PG.data.games.length) +
            ' games in the catalogue and counting.</p>' +
          '<div class="hero__actions">' +
            '<a class="btn btn--primary" href="#/signin">Create a free account</a>' +
            '<a class="btn" href="#/games">Browse the catalogue</a>' +
          '</div>' +
        '</div>' +
      '</section>';
    }

    /* Community-derived, so still local until Phase 7. summaryOf() reads the
       seeded ratings out of localStorage; there is nothing server-side to ask
       yet, and pretending otherwise would show every game at zero stars. */
    var byRating = PG.data.games.slice().map(function (g) {
      return { g: g, s: summaryOf(g) };
    }).filter(function (o) { return o.s.total > 400; })
      .sort(function (a, b) { return b.s.average - a.s.average; })
      .slice(0, 7);

    var feed = recentLogs(8);
    var reviews = popularReviews(4);

    /* The whole point of following: what your circle played, above the
       site-wide feed. Only worth a section when there is something in it. */
    var circleFeed = me ? store.logsFromFollowing(me.username)
      .filter(function (l) { return l.rating; })
      .sort(function (a, b) { return (b.date || '').localeCompare(a.date || ''); })
      .slice(0, 6) : [];

    return hero +
      '<section class="section">' + sectionHead('Trending this week', 'All games', '#/games') +
        ui.grid(trending, function (g) {
          return { log: me ? store.myLog(g.id) : null, rate: !!me };
        }) + '</section>' +
      (circleFeed.length ? '<section class="section">' +
        sectionHead('From people you follow', 'Your following feed',
          '#/activity' + toQuery({ who: 'following' })) +
        '<div class="feed">' + circleFeed.map(ui.feedRow).join('') + '</div></section>' : '') +
      '<section class="section">' + sectionHead('Just logged', 'All activity', '#/activity') +
        '<div class="feed">' + feed.map(ui.feedRow).join('') + '</div></section>' +
      '<section class="section">' +
        sectionHead('Popular reviews', 'More reviews',
          '#/activity' + toQuery({ show: 'reviews', sort: 'liked' })) +
        reviews.map(function (l) { return ui.review(l, reviewOpts(l, { showGame: true })); }).join('') +
      '</section>' +
      '<section class="section">' + sectionHead('Highest rated', 'Browse by rating',
        '#/games' + toQuery({ sort: 'rating' })) +
        ui.grid(byRating.map(function (o) { return o.g; }), function (g) {
          return {
            rating: summaryOf(g).average,
            log: me ? store.myLog(g.id) : null,
            rate: !!me
          };
        }) + '</section>';
  }

  /* ================================================================ browse */

  function selectField(label, name, options, current) {
    return '<label class="field"><span class="field__label">' + ui.esc(label) +
      '</span><select class="select" data-filter="' + name + '">' +
      options.map(function (o) {
        var value = o[0], text = o[1];
        return '<option value="' + ui.attr(value) + '"' +
          (String(value) === String(current) ? ' selected' : '') + '>' +
          ui.esc(text) + '</option>';
      }).join('') + '</select></label>';
  }

  /* Browse. The first view to go through the catalogue seam, so both halves of
     what Phase 4 changes are visible here: the data arrives as a promise, and
     the filter vocabulary now comes from the same source that validates it. */
  function viewGames(q) {
    return Promise.all([
      PG.catalogue.list({
        q: q.q, genre: q.genre, platform: q.platform, decade: q.decade,
        sort: q.sort, page: state.gamesPage
      }),
      PG.catalogue.facets()
    ]).then(function (both) {
      return renderGames(q, both[0], both[1]);
    });
  }

  function renderGames(q, page, facets) {
    var shown = page.games;
    var total = page.total;
    var me = store.currentUser();

    var genreOpts = [['', 'All genres']].concat(facets.genres.map(function (g) {
      return [g.name, g.name + ' (' + g.count + ')'];
    }));
    var platformOpts = [['', 'All platforms']].concat(facets.platforms.map(function (p) {
      return [p.name, p.name + ' (' + p.count + ')'];
    }));
    var decadeOpts = [['', 'Any year']].concat(facets.decades.map(function (d) {
      return [d.decade, d.decade + 's (' + d.count + ')'];
    }));

    var filters = '<div class="filters">' +
      '<label class="field field--grow"><span class="field__label">Title or studio</span>' +
        '<input class="input" type="search" data-filter="q" value="' +
        ui.attr(q.q || '') + '" placeholder="Search ' +
        ui.fmtNumber(facets.total) + ' games…"></label>' +
      selectField('Genre', 'genre', genreOpts, q.genre || '') +
      selectField('Platform', 'platform', platformOpts, q.platform || '') +
      selectField('Released', 'decade', decadeOpts, q.decade || '') +
      selectField('Sort by', 'sort', [
        ['popular', 'Popularity'],
        ['rating', 'Highest rated'],
        ['logged', 'Most logged'],
        ['critic', 'Critic score'],
        ['newest', 'Newest first'],
        ['oldest', 'Oldest first'],
        ['title', 'Title A–Z']
      ], q.sort || 'popular') +
      '<div class="count">' + ui.fmtNumber(total) +
        (total === 1 ? ' game' : ' games') + '</div>' +
      '</div>';

    var body = shown.length ?
      ui.grid(shown, function (g) {
        return {
          log: me ? store.myLog(g.id) : null,
          rate: !!me,
          flag: me && !store.myLog(g.id) && store.inBacklog(g.id) ? 'On your want to play list' : null
        };
      }) :
      ui.empty('No games match those filters', 'Try clearing the genre or platform.');

    var more = page.hasMore ?
      '<div class="more"><button class="btn" data-act="more-games">Show ' +
      Math.min(PAGE_SIZE, total - shown.length) + ' more</button></div>' : '';

    return '<div class="pagehead"><h1>Games</h1>' +
      '<p>Every game in the catalogue. Filter it down, then log what you have played.</p></div>' +
      filters + body + more;
  }

  /* =========================================================== game detail */

  function viewGame(id) {
    return PG.catalogue.get(id).then(function (game) {
      if (!game) return notFound('That game is not in the catalogue.');
      return renderGame(game);
    });
  }

  function renderGame(game) {
    var id = game.id;
    var me = store.currentUser();
    var mine = me ? store.myLog(game.id) : null;
    var summary = summaryOf(game);
    var logs = store.logsFor(game.id);
    var reviews = logs.filter(function (l) {
      return l.review && (!mine || l.id !== mine.id);
    }).sort(function (a, b) { return store.reviewLikes(b) - store.reviewLikes(a); });

    var wanted = me && store.inBacklog(game.id);

    /* Rate and set a play state without opening the log form. */
    var quick = me ? '<div class="gp__quick">' +
      '<div class="gp__quick-label">' +
        (mine && mine.rating ? 'Your rating' : 'Rate it') + '</div>' +
      ui.rateStrip(game.id, mine ? mine.rating : 0, 'rate--lg') +
      '<div class="gp__quick-label" style="margin-top:14px">Play state</div>' +
      '<div class="seg seg--stack" role="radiogroup" aria-label="Play state">' +
        store.states.map(function (s) {
          var on = !!mine && store.statusOf(mine) === s;
          return '<button type="button" class="seg__btn' + (on ? ' is-on' : '') +
            '" data-act="quick-state" data-game="' + ui.attr(game.id) +
            '" data-status="' + s + '" role="radio" aria-checked="' +
            (on ? 'true' : 'false') + '">' + ui.esc(ui.stateLabel[s]) + '</button>';
        }).join('') +
      '</div></div>' : '';

    var actions = quick + '<div class="gp__actions">' +
      '<button class="btn btn--primary btn--block" data-act="log" data-game="' +
        ui.attr(game.id) + '">' + ui.icon(mine ? 'check' : 'plus') +
        (mine ? 'Edit your log' : 'Log this game') + '</button>' +
      '<button class="btn btn--block ' + (wanted ? 'btn--soft' : '') +
        '" data-act="want" data-game="' + ui.attr(game.id) + '">' +
        (wanted ? ui.icon('check') + 'On your list' : ui.icon('clock') + 'Want to play') +
        '</button>' +
      '</div>';

    var related = data.games.filter(function (g) {
      return g.id !== game.id && g.genres[0] === game.genres[0];
    }).slice(0, 6);

    var facts = '<div class="facts">' +
      '<div class="fact"><div class="fact__k">Developer</div><div class="fact__v">' +
        ui.esc(game.developer) + '</div></div>' +
      '<div class="fact"><div class="fact__k">Genres</div><div class="chips">' +
        game.genres.map(function (n) {
          return '<a class="chip" href="#/games' + toQuery({ genre: n }) + '">' +
            ui.esc(n) + '</a>';
        }).join('') + '</div></div>' +
      '<div class="fact"><div class="fact__k">Platforms</div><div class="chips">' +
        game.platforms.map(function (n) {
          return '<a class="chip" href="#/games' + toQuery({ platform: n }) + '">' +
            ui.esc(n) + '</a>';
        }).join('') + '</div></div>' +
      (game.critic ? '<div class="fact"><div class="fact__k">Critic score</div>' +
        '<div class="chips"><span class="chip chip--score">' + game.critic +
        ' / 100</span></div></div>' : '') +
      '</div>';

    return '<div class="crumb"><a href="#/games">Games</a> <span>/</span> <span>' +
        ui.esc(game.title) + '</span></div>' +
      '<div class="gp">' +
        '<aside class="gp__aside">' +
          '<div class="gp__poster">' + ui.cover(game) + '</div>' + actions +
        '</aside>' +
        '<div class="gp__main">' +
          '<header class="gp__head">' +
            '<h1 class="gp__title">' + ui.esc(game.title) + '</h1>' +
            '<div class="gp__sub"><span>' + ui.esc(game.year) + '</span>' +
              '<span>Developed by <b>' + ui.esc(game.developer) + '</b></span></div>' +
            (game.blurb ? '<p class="gp__blurb">' + ui.esc(game.blurb) + '</p>' : '') +
          '</header>' +

          '<div class="panel"><div class="scoreboard">' +
            '<div><div class="scoreboard__big">' + ui.fmtRating(summary.average) +
              ' <span class="scoreboard__of">/ 5</span></div>' +
              ui.stars(summary.average, 'stars--lg') +
              '<div class="scoreboard__count">' + ui.fmtNumber(summary.total) +
              ' ratings from members</div></div>' +
            '<div>' + ui.histogram(summary) + '</div>' +
          '</div></div>' +

          facts +

          (mine ? '<section class="section" style="margin-top:38px">' +
            sectionHead('Your log') +
            ui.review(mine, reviewOpts(mine, { mine: true, showFinished: true })) +
            (!mine.review ? '<div class="review__foot" style="padding-left:48px">' +
              '<button class="linkbtn" data-act="log" data-game="' + ui.attr(game.id) +
              '">Add a review</button></div>' : '') +
            '</section>' : '') +

          '<section class="section" style="margin-top:38px">' +
            sectionHead('Reviews') +
            (reviews.length ? reviews.map(function (l) { return ui.review(l, reviewOpts(l)); }).join('') :
              ui.empty('No reviews yet', 'Be the first member to write one.')) +
          '</section>' +

          (related.length ? '<section class="section">' +
            sectionHead('More ' + game.genres[0], 'See all',
              '#/games' + toQuery({ genre: game.genres[0] })) +
            ui.grid(related, function (g) {
              return { log: me ? store.myLog(g.id) : null, rate: !!me };
            }, 'grid--tight') + '</section>' : '') +
        '</div>' +
      '</div>';
  }

  /* ================================================================ member */

  /* The game sections on a profile. One table so a section's cap and the page
     that shows the rest cannot drift apart, and so "See more" turns up exactly
     when a section is holding something back. */
  var MEMBER_LISTS = {
    playing: {
      title: 'Currently playing', limit: 12,
      blurb: function (isMe, name) {
        return 'What ' + (isMe ? 'you have' : name + ' has') + ' on the go right now.';
      }
    },
    favorites: {
      title: 'Favorites', limit: 6,
      blurb: function (isMe, name) {
        return 'Everything ' + (isMe ? 'you have' : name + ' has') +
          ' rated four and a half stars or better.';
      }
    },
    logged: {
      title: 'Recently logged', limit: 12,
      blurb: function (isMe, name) {
        return 'Every game in ' + (isMe ? 'your' : name + '’s') +
          ' diary, newest first.';
      }
    },
    abandoned: {
      title: 'Gave up on', limit: 12,
      blurb: function (isMe, name) {
        return 'Games ' + (isMe ? 'you' : name) + ' started and put down.';
      }
    },
    backlog: {
      title: 'Want to play', limit: 12,
      blurb: function (isMe, name) {
        return 'Games ' + (isMe ? 'you want' : name + ' wants') + ' to get to.';
      }
    }
  };

  function ratedLogs(username) {
    return store.logsBy(username).filter(function (l) { return l.rating; });
  }

  /* Four of these are diary entries; the backlog is plain games with no entry
     behind them. The shape travels with the list rather than being guessed at
     by whoever renders it. */
  function memberGames(username, which) {
    switch (which) {
      case 'playing':
        return { kind: 'logs', items: store.logsByStatus(username, 'playing') };
      case 'abandoned':
        return { kind: 'logs', items: store.logsByStatus(username, 'abandoned') };
      case 'favorites':
        return { kind: 'logs', items: ratedLogs(username)
          .filter(function (l) { return l.rating >= 4.5; })
          .sort(function (a, b) {
            return b.rating - a.rating ||
              (PG.catalogue.resolve(a.game).rank - PG.catalogue.resolve(b.game).rank);
          }) };
      case 'logged':
        return { kind: 'logs', items: ratedLogs(username).sort(function (a, b) {
          return (b.date || '').localeCompare(a.date || '');
        }) };
      case 'backlog':
        return { kind: 'games', items: store.backlogFor(username)
          .map(function (id) { return PG.catalogue.resolve(id); }).filter(Boolean) };
    }
    return null;
  }

  /* A falsy limit means show the lot. */
  function gamesGrid(list, limit, isMe) {
    var items = limit ? list.items.slice(0, limit) : list.items;
    if (list.kind === 'games') {
      return ui.grid(items, function () {
        return { sub: false, rate: isMe };
      }, 'grid--tight');
    }
    return ui.grid(items.map(function (l) { return PG.catalogue.resolve(l.game); }).filter(Boolean),
      function (g) {
        var l = items.filter(function (x) { return x.game === g.id; })[0];
        /* Only the owner gets live stars — on someone else's shelf the rating
           shown is theirs, not yours. */
        return { log: l, sub: false, rate: isMe };
      });
  }

  /* One profile section: a capped grid plus a "See more" link to the full list,
     the link appearing only when the cap is actually hiding something — a link
     to an identical page is a dead end. Pass emptyHtml for a section that
     should still show up when there is nothing in it. */
  function memberSection(member, which, isMe, emptyHtml) {
    var meta = MEMBER_LISTS[which];
    var list = memberGames(member.username, which);
    if (!list.items.length) {
      return emptyHtml ? '<section class="section">' + sectionHead(meta.title) +
        emptyHtml + '</section>' : '';
    }
    return '<section class="section">' +
      sectionHead(meta.title, list.items.length > meta.limit ? 'See more' : '',
        '#/member/' + ui.attr(member.username) + '/' + which) +
      gamesGrid(list, meta.limit, isMe) +
      '</section>';
  }

  function viewMember(username) {
    var member = store.findMember(username);
    if (!member) return notFound('No member with that name.');
    var me = store.currentUser();
    var isMe = !!me && me.username === member.username;
    var stats = store.memberStats(member.username);

    var counts = store.followCounts(member.username);
    /* Shown on someone else's profile only — "follows you" is information about
       them, and on your own page it would just say it about everybody. */
    var followsMe = !isMe && !!me &&
      store.followingOf(member.username).indexOf(me.username) > -1;

    return '<header class="profile__head">' +
        ui.avatar(member, 'avatar--lg') +
        '<div class="profile__id"><h1 class="profile__name">' + ui.esc(member.name) + '</h1>' +
        '<div class="profile__handle">@' + ui.esc(member.username) +
          (isMe ? ' · this is you' : '') +
          (followsMe ? ' <span class="mcard__tag">Follows you</span>' : '') + '</div>' +
        (member.bio ? '<p class="profile__bio">' + ui.esc(member.bio) + '</p>' : '') +
        '<div class="profile__counts">' +
          '<a href="#/member/' + ui.attr(member.username) + '/followers"><b>' +
            ui.fmtNumber(counts.followers) + '</b> ' +
            (counts.followers === 1 ? 'follower' : 'followers') + '</a>' +
          '<a href="#/member/' + ui.attr(member.username) + '/following"><b>' +
            ui.fmtNumber(counts.following) + '</b> following</a>' +
        '</div>' +
        '</div>' +
        (isMe ? '<a class="btn profile__act" href="#/settings">Edit profile</a>' :
          '<div class="profile__act">' +
            (me && store.canMessage(me.username, member.username) ?
              '<a class="btn" href="#/messages/' + ui.attr(member.username) + '">' +
                'Message</a>' : '') +
            ui.followButton(member.username) + '</div>') +
      '</header>' +
      /* Five headline figures; the play states go underneath as a proportion
         bar rather than as three more boxes. Labels are kept to one short word
         each so they never wrap — the old two-word labels went to two lines at
         every width below 1220px. */
      ui.statBlock([
        [stats.logged, 'Logged'],
        [ui.fmtNumber(stats.hours), 'Hours', 'h'],
        [ui.fmtRating(stats.average), 'Average', stats.logged ? '★' : ''],
        [stats.reviews, 'Reviews'],
        [stats.backlog, 'Want to play']
      ], stats) +
      /* Only worth showing when there are two libraries to compare. */
      (!isMe && me && stats.logged && store.memberStats(me.username).logged ?
        ui.tastePanel(store.tasteMatch(me.username, member.username), member) : '') +
      '<div style="height:34px"></div>' +
      /* No Reviews section: a review belongs next to the game it is about, and
         it already shows on the game page and in Activity. The Reviews figure
         in the stat block is what a profile needs to say about them. */
      memberSection(member, 'playing', isMe) +
      memberSection(member, 'favorites', isMe) +
      memberSection(member, 'logged', isMe,
        ui.empty(isMe ? 'Your diary is empty' : 'Nothing logged yet',
          isMe ? 'Find a game you have played and give it a rating.' : '')) +
      memberSection(member, 'abandoned', isMe) +
      memberSection(member, 'backlog', isMe);
  }

  /* #/member/<name>/<list> — the whole of one profile section. */
  function viewMemberList(username, which) {
    var member = store.findMember(username);
    if (!member) return notFound('No member with that name.');
    var meta = MEMBER_LISTS[which];
    if (!meta) return notFound('That page does not exist.');

    var me = store.currentUser();
    var isMe = !!me && me.username === member.username;
    var list = memberGames(member.username, which);

    return '<div class="crumb"><a href="#/member/' + ui.attr(member.username) + '">← ' +
        ui.esc(member.name) + '</a></div>' +
      '<div class="pagehead"><h1>' + ui.esc(meta.title) + '</h1><p>' +
        ui.esc(meta.blurb(isMe, member.name)) + '</p></div>' +
      (list.items.length ? gamesGrid(list, 0, isMe) : ui.empty('Nothing here yet'));
  }

  /* #/member/<name>/followers and /following. Same card as the members list, so
     the follow button and the "follows you" tag come along for free. */
  function viewFollowList(username, which) {
    var member = store.findMember(username);
    if (!member) return notFound('No member with that name.');
    if (which !== 'followers' && which !== 'following') {
      return notFound('That page does not exist.');
    }
    var me = store.currentUser();
    var isMe = !!me && me.username === member.username;
    var handles = which === 'followers' ?
      store.followersOf(member.username) : store.followingOf(member.username);
    var people = handles.map(store.findMember).filter(Boolean);

    var heading = which === 'followers' ? 'Followers' : 'Following';
    var blurb = which === 'followers' ?
      (isMe ? 'Members keeping up with your diary.' :
        'Members keeping up with ' + member.name + '.') :
      (isMe ? 'The diaries in your Following feed.' :
        member.name + ' keeps up with these members.');

    var empty = which === 'followers' ?
      ui.empty(isMe ? 'No followers yet' : 'No followers yet',
        isMe ? 'Write a review or two — members find each other through the feed.' :
          'Nobody is following ' + member.name + ' yet.') :
      ui.empty(isMe ? 'You are not following anyone' : 'Not following anyone yet',
        isMe ? 'Find members on the Members page and follow a few to build your feed.' :
          member.name + ' has not followed anyone yet.');

    return '<div class="crumb"><a href="#/member/' + ui.attr(member.username) + '">← ' +
        ui.esc(member.name) + '</a></div>' +
      '<div class="pagehead"><h1>' + heading + '</h1><p>' + ui.esc(blurb) + '</p></div>' +
      (people.length ? ui.memberList(people, {
        each: function (m) {
          return {
            followsYou: !!me && m.username !== me.username &&
              store.followingOf(m.username).indexOf(me.username) > -1
          };
        }
      }) : empty) +
      (people.length && which === 'following' && isMe ?
        '<div class="more"><a class="btn" href="#/activity' +
          toQuery({ who: 'following' }) + '">Open your Following feed</a></div>' : '');
  }

  function viewMembers(q) {
    var me = store.currentUser();
    var members = store.allMembers();
    var term = (q.q || '').trim().toLowerCase();

    var matches = !term ? members : members.filter(function (m) {
      return (m.username + ' ' + m.name + ' ' + (m.bio || ''))
        .toLowerCase().indexOf(term) > -1;
    });

    var search = '<div class="filters">' +
      '<label class="field field--grow"><span class="field__label">Find a member</span>' +
        '<input class="input" type="search" data-filter="q" value="' + ui.attr(q.q || '') +
        '" placeholder="Search by name, handle or bio…"></label>' +
      '<div class="count">' + (term ?
        ui.fmtNumber(matches.length) + ' of ' + ui.fmtNumber(members.length) :
        ui.fmtNumber(members.length)) + ' members</div>' +
      '</div>';

    if (!matches.length) {
      return '<div class="pagehead"><h1>Members</h1></div>' + search +
        ui.empty('No members match “' + ui.esc(q.q) + '”',
          'Try part of a username, a display name, or something from a bio.');
    }

    /* Card options are the same everywhere on this page: whether they follow
       you, and how closely your libraries line up. */
    function cardOpts(m) {
      var mine = me && store.memberStats(me.username).logged;
      return {
        followsYou: !!me && m.username !== me.username &&
          store.followingOf(m.username).indexOf(me.username) > -1,
        match: me && mine && m.username !== me.username ?
          store.tasteMatch(me.username, m.username) : null
      };
    }

    /* Recommendations only make sense once you have rated something, and there
       is no point recommending people you already follow. */
    var suggestions = me && store.memberStats(me.username).logged && !term ?
      store.similarMembers(me.username, { limit: 3, excludeFollowing: true }) : [];

    var recommended = suggestions.length ? '<section class="section">' +
      sectionHead('Closest to your taste') +
      '<p class="section__note">Members you are not following yet, ranked by how ' +
        'much your ratings and the genres you play line up.</p>' +
      ui.memberList(suggestions.map(function (s) { return s.member; }), {
        each: cardOpts
      }) +
      '</section>' : '';

    return '<div class="pagehead"><h1>Members</h1>' +
      '<p>Everyone keeping a diary on Postgame. The demo accounts all share the ' +
      'password <b>' + ui.esc(store.demoPassword) + '</b> — sign in as any of them to look around.</p></div>' +
      recommended +
      (recommended ? sectionHead('Everyone') : '') +
      search +
      ui.memberList(matches, { each: cardOpts });
  }

  /* ====================================================== activity/reviews */

  function segLink(label, href, on) {
    return '<a class="seg__btn' + (on ? ' is-on' : '') + '" href="' + href + '"' +
      (on ? ' aria-current="page"' : '') + '>' + ui.esc(label) + '</a>';
  }

  /* Activity and Reviews were the same list rendered twice — every review is a
     diary entry — so they are one page with a filter and a sort between them.
     That also gives two views neither page offered: newest reviews, and the
     most-liked entries regardless of whether anyone wrote anything. */
  function viewActivity(q) {
    var reviewsOnly = q.show === 'reviews';
    var byLikes = q.sort === 'liked';
    var me = store.currentUser();
    /* Following is only a real view when there is someone to follow with. */
    var following = q.who === 'following' && !!me;
    var circle = following ? store.followingOf(me.username) : [];

    var logs = (following ? store.logsFromFollowing(me.username) : store.allLogs())
      .filter(function (l) {
        return reviewsOnly ? !!l.review : !!l.rating;
      });
    logs.sort(byLikes ?
      function (a, b) { return store.reviewLikes(b) - store.reviewLikes(a); } :
      function (a, b) { return (b.date || '').localeCompare(a.date || ''); });

    var shown = logs.slice(0, FEED_PAGE * state.feedPage);

    function link(change) {
      var next = { who: q.who || '', show: q.show || '', sort: q.sort || '' };
      Object.keys(change).forEach(function (k) { next[k] = change[k]; });
      return '#/activity' + toQuery(next);
    }

    var controls = '<div class="filters filters--feed">' +
      (me ? '<div class="seg">' +
        segLink('Everyone', link({ who: '' }), !following) +
        segLink('Following', link({ who: 'following' }), following) +
      '</div>' : '') +
      '<div class="seg">' +
        segLink('Everything', link({ show: '' }), !reviewsOnly) +
        segLink('Reviews only', link({ show: 'reviews' }), reviewsOnly) +
      '</div>' +
      '<div class="seg">' +
        segLink('Newest', link({ sort: '' }), !byLikes) +
        segLink('Most liked', link({ sort: 'liked' }), byLikes) +
      '</div>' +
      '<div class="count">' + ui.fmtNumber(logs.length) +
        (reviewsOnly ? ' reviews' : ' entries') + '</div>' +
      '</div>';

    var body;
    if (following && !circle.length) {
      body = ui.empty('You are not following anyone yet',
        'Follow a few members and their diary entries collect here.') +
        '<div class="more"><a class="btn btn--primary" href="#/members">Find members</a></div>';
    } else if (!shown.length) {
      body = following ?
        ui.empty('Nothing from your circle yet',
          'The ' + circle.length + ' ' + (circle.length === 1 ? 'member' : 'members') +
          ' you follow have not logged anything matching this filter.') :
        ui.empty('Nothing here yet', 'Log a game and it turns up in this feed.');
    } else if (reviewsOnly) {
      /* Written reviews get the full card: prose, likes, the lot. */
      body = shown.map(function (l) { return ui.review(l, reviewOpts(l, { showGame: true })); }).join('');
    } else {
      /* Most entries are a rating and nothing else, so they read better as a
         compact feed than as a column of near-empty cards. */
      body = '<div class="feed">' + shown.map(ui.feedRow).join('') + '</div>';
    }

    return '<div class="pagehead"><h1>Activity</h1>' +
      '<p>' + (following ?
        'Diary entries from the ' + circle.length + ' ' +
          (circle.length === 1 ? 'member' : 'members') + ' you follow.' :
        'Every diary entry on Postgame — ratings, replays and reviews.') +
      '</p></div>' +
      controls + body +
      (shown.length < logs.length ?
        '<div class="more"><button class="btn" data-act="more-feed">Show ' +
        Math.min(FEED_PAGE, logs.length - shown.length) + ' more</button></div>' : '');
  }

  /* ================================================================== auth */

  function viewAuth() {
    var me = store.currentUser();
    if (me) {
      return '<div class="auth"><div class="auth__card">' +
        '<h1 class="auth__title">You are signed in</h1>' +
        '<p class="auth__sub">Signed in as <b>' + ui.esc(me.name) + '</b> (@' +
          ui.esc(me.username) + ').</p>' +
        '<div style="display:grid;gap:10px;margin-top:20px">' +
          '<a class="btn btn--primary btn--block" href="#/member/' +
            ui.attr(me.username) + '">Go to your diary</a>' +
          '<button class="btn btn--block" data-act="signout">Sign out</button>' +
        '</div></div></div>';
    }

    /* autocomplete values are deliberate, not decorative. These fields carried
       autocomplete="off" until the Phase 5 ASVS review (V2.1.11), which asks that password
       managers be permitted rather than fought. "off" on a password field does not make
       anything safer — every current browser ignores it for credentials anyway — and it
       does discourage the one habit that most reduces credential reuse. The search box
       keeps its "off": it is not a credential and the suggestion dropdown is our own. */
    var inTab = state.authTab === 'in';
    var demos = data.members.slice(0, 3);

    var form = inTab ?
      '<form class="form" id="signin-form" novalidate>' +
        (state.authError ? '<div class="formerror">' + ui.esc(state.authError) + '</div>' : '') +
        '<label class="field"><span class="field__label">Username</span>' +
          '<input class="input" name="username" autocomplete="username" data-autofocus placeholder="pixelvagrant"></label>' +
        '<label class="field"><span class="field__label">Password</span>' +
          '<input class="input" name="password" type="password" autocomplete="current-password" placeholder="••••••••"></label>' +
        '<button class="btn btn--primary btn--block" type="submit">Sign in</button>' +
      '</form>' :
      '<form class="form" id="signup-form" novalidate>' +
        (state.authError ? '<div class="formerror">' + ui.esc(state.authError) + '</div>' : '') +
        '<label class="field"><span class="field__label">Username</span>' +
          '<input class="input" name="username" autocomplete="username" data-autofocus placeholder="letters, numbers, underscores"></label>' +
        '<label class="field"><span class="field__label">Display name</span>' +
          '<input class="input" name="name" autocomplete="name" placeholder="What should we call you?"></label>' +
        '<label class="field"><span class="field__label">Password</span>' +
          '<input class="input" name="password" type="password" autocomplete="new-password" placeholder="at least 4 characters"></label>' +
        '<button class="btn btn--primary btn--block" type="submit">Create account</button>' +
      '</form>';

    return '<div class="auth"><div class="auth__card">' +
      '<svg class="auth__mark" aria-hidden="true"><use href="#icon-logo"></use></svg>' +
      '<h1 class="auth__title">' + (inTab ? 'Welcome back' : 'Start your diary') + '</h1>' +
      '<p class="auth__sub">' + (inTab ?
        'Sign in to pick up your diary where you left off.' :
        'Free, instant, and stored only in this browser.') + '</p>' +
      '<div class="tabs" role="tablist">' +
        '<button role="tab" class="' + (inTab ? 'is-on' : '') +
          '" data-act="auth-tab" data-tab="in">Sign in</button>' +
        '<button role="tab" class="' + (!inTab ? 'is-on' : '') +
          '" data-act="auth-tab" data-tab="up">Create account</button>' +
      '</div>' +
      form +
      '<div class="demoaccounts">' +
        '<div class="demoaccounts__t">Test accounts — one click</div>' +
        '<div class="demolist">' + demos.map(function (m) {
          return '<button class="demo" data-act="demo" data-user="' + ui.attr(m.username) +
            '" aria-label="Sign in as ' + ui.attr(m.username) + '">' +
            ui.avatar(m, 'avatar--sm') +
            '<span class="demo__meta"><span class="demo__u">' + ui.esc(m.username) +
            '</span><span class="demo__d"> · ' + ui.esc(store.demoPassword) + '</span>' +
            '<span class="demo__name">' + ui.esc(m.name) + '</span></span>' +
            '<span class="demo__go">Sign in</span></button>';
        }).join('') + '</div>' +
        '<p class="note">Every demo member uses the password <b>' +
          ui.esc(store.demoPassword) + '</b>. Accounts on Postgame are a local ' +
          'demo: they live in this browser\'s storage, are not encrypted, and are ' +
          'never sent anywhere. Do not reuse a real password here.</p>' +
      '</div>' +
      '</div></div>';
  }

  /* ============================================================== messages */

  function signInWall(title, blurb) {
    return '<div class="auth"><div class="auth__card">' +
      '<h1 class="auth__title">' + ui.esc(title) + '</h1>' +
      '<p class="auth__sub">' + ui.esc(blurb) + '</p>' +
      '<div style="display:grid;gap:10px;margin-top:20px">' +
        '<a class="btn btn--primary btn--block" href="#/signin">Sign in</a>' +
        '<a class="btn btn--block" href="#/">Back to the home page</a>' +
      '</div></div></div>';
  }

  function viewMessages() {
    var me = store.currentUser();
    if (!me) {
      return signInWall('Messages',
        'Sign in to read and write messages.');
    }
    /* Only real conversations. Everyone you *could* write to lives behind the
       New message button instead of padding this list with empty rows. */
    var list = store.conversations();
    var canWrite = recipients().length;

    return '<div class="pagehead pagehead--row">' +
        '<div><h1>Messages</h1>' +
        '<p>Private to this browser. You can write to members who follow you ' +
        'back — following someone does not put you in their inbox.</p></div>' +
        (canWrite ? '<button class="btn btn--primary pagehead__act" ' +
          'data-act="new-message">' + ui.icon('plus') + 'New message</button>' : '') +
      '</div>' +
      (list.length ?
        '<div class="convlist">' + list.map(ui.conversationRow).join('') + '</div>' :
        canWrite ?
          ui.empty('No conversations yet',
            'Start one with any member who follows you back.') +
          '<div class="more"><button class="btn btn--primary" data-act="new-message">' +
            ui.icon('plus') + 'New message</button></div>' :
          ui.empty('No one to message yet',
            'Messaging opens up when you and another member follow each other.') +
          '<div class="more"><a class="btn btn--primary" href="#/members">Find members</a></div>');
  }

  /* Everyone the signed-in member is allowed to write to, with any existing
     thread noted so the picker can say so. */
  function recipients() {
    var me = store.currentUser();
    if (!me) return [];
    return store.followingOf(me.username)
      .filter(function (u) { return store.canMessage(me.username, u); })
      .map(function (u) {
        return {
          member: store.findMember(u),
          existing: store.messagesWith(u, me.username).length
        };
      })
      .filter(function (r) { return !!r.member; })
      .sort(function (a, b) {
        return a.member.name.localeCompare(b.member.name);
      });
  }

  function openNewMessage() {
    var people = recipients();
    if (!people.length) {
      ui.toast('Follow someone who follows you back first', true);
      go('#/members');
      return;
    }

    var rows = people.map(function (r) {
      var m = r.member;
      /* Pre-lowered haystack so filtering as you type is a substring test and
         nothing has to be recomputed per keystroke. */
      var hay = (m.name + ' ' + m.username + ' ' + (m.bio || '')).toLowerCase();
      return '<a class="pick" href="#/messages/' + ui.attr(m.username) +
        '" data-search="' + ui.attr(hay) + '">' +
        ui.avatar(m, 'avatar--sm') +
        '<span class="pick__meta">' +
          '<span class="pick__name">' + ui.esc(m.name) + '</span>' +
          '<span class="pick__handle">@' + ui.esc(m.username) + '</span>' +
        '</span>' +
        (r.existing ? '<span class="pick__tag">Existing thread</span>' : '') +
        '</a>';
    }).join('');

    ui.openModal(
      '<div class="modal__head">' +
        '<div><h2 class="modal__title">New message</h2>' +
        /* The verb agrees too: "1 member follows", "5 members follow". */
        '<div class="modal__sub">' + (people.length === 1 ?
          '1 member follows you back' :
          ui.fmtNumber(people.length) + ' members follow you back') + '</div></div>' +
        '<button class="modal__x" data-act="close-modal" aria-label="Close">✕</button>' +
      '</div>' +
      '<div class="modal__body">' +
        '<label class="field"><span class="sr-only">Search members</span>' +
          '<input class="input" id="pick-search" type="search" autocomplete="off" ' +
            'data-autofocus placeholder="Search by name or handle…"></label>' +
        '<div class="picklist" id="pick-list">' + rows + '</div>' +
        '<div class="pick__none" id="pick-none" hidden>No one matches that.</div>' +
      '</div>');
  }

  /* Filters the picker in place. A re-render would close the modal and take the
     caret with it, so this touches the DOM directly. */
  function filterRecipients(term) {
    var list = document.getElementById('pick-list');
    if (!list) return;
    var t = String(term || '').trim().toLowerCase();
    var shown = 0;
    Array.prototype.forEach.call(list.querySelectorAll('.pick'), function (row) {
      var on = !t || row.dataset.search.indexOf(t) > -1;
      row.hidden = !on;
      if (on) shown++;
    });
    var none = document.getElementById('pick-none');
    if (none) none.hidden = shown > 0;
  }

  function viewThread(handle) {
    var me = store.currentUser();
    if (!me) {
      return signInWall('Messages', 'Sign in to read and write messages.');
    }
    var them = store.findMember(handle);
    if (!them) return notFound('No member with that name.');
    if (them.username === me.username) {
      return notFound('You cannot message yourself.');
    }

    var thread = store.messagesWith(them.username, me.username);
    var open = store.canMessage(me.username, them.username);
    /* Marking the thread read is a side effect of *visiting*, so it happens in
       the router once this markup is committed — see afterRender(). Building a
       view stays free of writes, which means a re-render for any other reason
       cannot quietly change stored state. */

    var compose = open ?
      '<form class="mform" data-to="' + ui.attr(them.username) + '">' +
        '<textarea class="textarea textarea--sm" name="body" rows="2" maxlength="' +
          store.messageMax + '" placeholder="Message ' + ui.attr(them.name) +
          '…" aria-label="Write a message" data-autofocus></textarea>' +
        '<div class="cform__foot">' +
          '<span class="charcount">0 / ' + store.messageMax + '</span>' +
          '<button class="btn btn--primary btn--sm" type="submit">Send</button>' +
        '</div>' +
      '</form>' :
      /* "No longer" only if there is a history to have lost — otherwise it
         implies a conversation that never happened. */
      '<div class="mform__shut">' +
        '<b>' + (thread.length ?
          'You can no longer write to ' + ui.esc(them.name) + '.' :
          'You cannot message ' + ui.esc(them.name) + ' yet.') + '</b> ' +
        'Messaging needs you both to be following each other. ' +
        (store.isFollowing(them.username) ?
          'You follow them; they have not followed you back.' :
          store.followingOf(them.username).indexOf(me.username) > -1 ?
            'They follow you — follow them back to open this up.' :
            'Neither of you is following the other.') +
        '<div class="mform__do">' + ui.followButton(them.username, 'btn--sm') + '</div>' +
      '</div>';

    return '<div class="crumb"><a href="#/messages">← Messages</a></div>' +
      '<header class="thread__head">' +
        '<a href="#/member/' + ui.attr(them.username) + '" aria-label="' +
          ui.attr(them.name) + '">' + ui.avatar(them) + '</a>' +
        '<div>' +
          /* The person is what this page is about, so their name is its heading.
             It was the only route without an h1. */
          '<h1 class="thread__name"><a href="#/member/' + ui.attr(them.username) + '">' +
            ui.esc(them.name) + '</a></h1>' +
          '<div class="thread__handle">@' + ui.esc(them.username) + '</div>' +
        '</div>' +
      '</header>' +
      '<div class="msgs">' +
        (thread.length ? ui.messageList(thread, me.username) :
          '<p class="msgs__none">No messages yet. Say something.</p>') +
      '</div>' +
      compose;
  }

  /* ============================================================== settings */

  /* Ten hues spaced round the wheel. A free colour picker would let you choose
     something illegible in one theme or the other; these are all checked. */
  var HUES = [268, 320, 356, 22, 44, 88, 158, 190, 220, 244];

  /* The theme belongs to the browser, not to an account, so the appearance card
     renders whether or not anyone is signed in. Only the profile and account
     cards need a session. */
  function appearanceCard() {
    var mode = PG.theme.get();
    return '<section class="card">' +
      '<h2 class="card__t">Appearance</h2>' +
      '<p class="card__d">Applies to this browser, not to your account.</p>' +
      '<div class="setrow">' +
        '<div><div class="setrow__k">Theme</div>' +
          '<div class="setrow__d">System follows your device setting' +
            (mode === 'system' ? ' — currently ' + PG.theme.resolved() : '') +
            '.</div></div>' +
        '<div class="seg" role="group" aria-label="Theme">' +
          PG.theme.modes.map(function (m) {
            return '<button class="seg__btn' + (m === mode ? ' is-on' : '') +
              '" type="button" data-act="set-theme" data-theme="' + m +
              '" aria-pressed="' + (m === mode ? 'true' : 'false') + '">' +
              ui.esc(PG.theme.labels[m]) + '</button>';
          }).join('') +
        '</div>' +
      '</div>' +
    '</section>';
  }

  /* The content filter is deliberately not surfaced here. It is always on and
     not switchable, so a card for it would be a setting you cannot set — and
     advertising the rules is a handy starting point for anyone minded to work
     around them. It stays inspectable from the console via
     PG.moderate.blockedCount() and PG.moderate.test('word'). */

  /* Browser-level, so it sits beside Appearance and shows signed out. */
  function soundCard() {
    var on = PG.sound.get();
    return '<section class="card">' +
      '<h2 class="card__t">Sound</h2>' +
      '<p class="card__d">Short cues when you follow someone or save a diary ' +
        'entry. Nothing plays unless you clicked something.</p>' +
      '<div class="setrow">' +
        '<div><div class="setrow__k">Sound effects</div>' +
          '<div class="setrow__d">Stored in this browser.</div></div>' +
        '<div class="seg" role="group" aria-label="Sound effects">' +
          '<button class="seg__btn' + (on ? '' : ' is-on') + '" type="button" ' +
            'data-act="set-sound" data-on="0" aria-pressed="' +
            (on ? 'false' : 'true') + '">Off</button>' +
          '<button class="seg__btn' + (on ? ' is-on' : '') + '" type="button" ' +
            'data-act="set-sound" data-on="1" aria-pressed="' +
            (on ? 'true' : 'false') + '">On</button>' +
        '</div>' +
      '</div>' +
    '</section>';
  }

  function viewSettings() {
    var me = store.currentUser();
    if (!me) {
      return '<div class="settings">' +
        '<div class="pagehead"><h1>Settings</h1></div>' +
        appearanceCard() + soundCard() +
        '<section class="card">' +
          '<h2 class="card__t">Profile</h2>' +
          '<p class="card__d">Sign in to set a display name, a bio and a ' +
            'profile picture.</p>' +
          '<a class="btn btn--primary" href="#/signin">Sign in</a>' +
        '</section>' +
      '</div>';
    }

    var bio = me.bio || '';

    var profile = '<section class="card">' +
      '<h2 class="card__t">Profile</h2>' +
      '<p class="card__d">How you appear on your diary, your reviews and the ' +
        'members list.</p>' +
      (state.settingsError ?
        '<div class="formerror" style="margin-bottom:16px">' +
          ui.esc(state.settingsError) + '</div>' : '') +
      '<form class="form" id="profile-form" novalidate>' +
        '<div class="pic">' +
          ui.avatar(me, 'avatar--xl') +
          '<div class="pic__do">' +
            '<div class="pic__row">' +
              '<span class="btn btn--sm filebtn">' + ui.icon('plus') +
                (me.avatar ? 'Replace picture' : 'Upload a picture') +
                '<input type="file" id="avatar-input" accept="image/*" ' +
                  'aria-label="Upload a profile picture"></span>' +
              (me.avatar ? '<button class="btn btn--sm btn--danger" type="button" ' +
                'data-act="drop-avatar">Remove</button>' : '') +
            '</div>' +
            '<div class="pic__hint">Square works best. Cropped to the centre and ' +
              'stored in this browser — it is never uploaded anywhere.</div>' +
          '</div>' +
        '</div>' +
        (me.avatar ? '' :
          '<label class="field"><span class="field__label">Avatar colour</span>' +
            '<div class="hues">' + HUES.map(function (h) {
              return '<button type="button" class="hue' +
                (Number(me.hue) === h ? ' is-on' : '') + '" style="--h:' + h +
                '" data-act="set-hue" data-hue="' + h +
                '" aria-label="Avatar colour ' + h + ' degrees"' +
                (Number(me.hue) === h ? ' aria-pressed="true"' : '') + '></button>';
            }).join('') + '</div></label>') +
        '<label class="field"><span class="field__label">Display name</span>' +
          '<input class="input" name="name" autocomplete="off" maxlength="' +
            store.nameMax + '" value="' + ui.attr(me.name) + '"></label>' +
        '<label class="field"><span class="field__label">Bio</span>' +
          '<textarea class="textarea" name="bio" maxlength="' + store.bioMax +
            '" placeholder="What do you play?">' + ui.esc(bio) + '</textarea>' +
          '<span class="charcount">' + bio.length + ' / ' + store.bioMax +
          '</span></label>' +
        '<button class="btn btn--primary" type="submit">Save profile</button>' +
      '</form>' +
    '</section>';

    var stats = store.memberStats(me.username);
    var account = '<section class="card">' +
      '<h2 class="card__t">Account</h2>' +
      '<p class="card__d">Postgame is a local demo. Everything here lives in ' +
        'this browser only and is never sent anywhere.</p>' +
      '<div class="setrow">' +
        '<div><div class="setrow__k">Username</div>' +
          '<div class="setrow__d">Permanent — your reviews are filed under it.</div></div>' +
        '<div class="setrow__v">@' + ui.esc(me.username) + '</div>' +
      '</div>' +
      '<div class="setrow">' +
        '<div><div class="setrow__k">Diary</div>' +
          '<div class="setrow__d">' + stats.logged + ' logged · ' + stats.reviews +
            ' reviews · ' + ui.fmtNumber(stats.hours) + ' hours</div></div>' +
        '<a class="btn btn--sm" href="#/member/' + ui.attr(me.username) +
          '">View profile</a>' +
      '</div>' +
      '<div class="setrow">' +
        '<div><div class="setrow__k">Session</div>' +
          '<div class="setrow__d">Your diary stays in this browser when you ' +
            'sign out.</div></div>' +
        '<button class="btn btn--sm btn--danger" data-act="signout">Sign out</button>' +
      '</div>' +
    '</section>';

    return '<div class="settings">' +
      '<div class="pagehead"><h1>Settings</h1></div>' +
      profile + appearanceCard() + soundCard() + account +
    '</div>';
  }

  /* Reads a chosen image into a square data URL small enough to keep in
     localStorage. Downscaling in a canvas is the only option here: there is no
     server to resize on, and a 4MB photo would blow the storage quota. */
  function readAvatar(file, done) {
    var SIZE = 192;
    if (!file) return;
    if (!/^image\//.test(file.type)) {
      done({ error: 'That is not an image file.' });
      return;
    }
    if (file.size > 12 * 1024 * 1024) {
      done({ error: 'That image is over 12MB. Try a smaller one.' });
      return;
    }
    var reader = new window.FileReader();
    reader.onerror = function () { done({ error: 'Could not read that file.' }); };
    reader.onload = function () {
      var img = new window.Image();
      img.onerror = function () { done({ error: 'That image could not be decoded.' }); };
      img.onload = function () {
        var side = Math.min(img.width, img.height);
        if (!side) { done({ error: 'That image has no size.' }); return; }
        var canvas = document.createElement('canvas');
        canvas.width = SIZE;
        canvas.height = SIZE;
        var ctx = canvas.getContext('2d');
        /* A transparent PNG would otherwise come out of toDataURL('jpeg') with
           black wherever it was see-through. */
        ctx.fillStyle = '#ffffff';
        ctx.fillRect(0, 0, SIZE, SIZE);
        ctx.drawImage(img, (img.width - side) / 2, (img.height - side) / 2,
          side, side, 0, 0, SIZE, SIZE);
        try {
          done({ url: canvas.toDataURL('image/jpeg', 0.82) });
        } catch (e) {
          done({ error: 'That image could not be converted.' });
        }
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  }

  /* Whatever is currently typed into the profile form. Picking a colour or
     dropping a picture re-renders the page, which would otherwise throw away an
     unsaved name or bio — so those edits carry the visible values along. */
  function profileFormValues() {
    var form = document.getElementById('profile-form');
    if (!form) return {};
    return { name: form.name.value, bio: form.bio.value };
  }

  function saveProfile(patch, message, refocus) {
    var res = store.updateProfile(patch);
    if (res.error) {
      state.settingsError = res.error;
      render(true);
      return false;
    }
    state.settingsError = '';
    render(true);
    /* The control that caused this was replaced by the re-render; put the
       keyboard back on its replacement. */
    if (refocus) {
      var again = document.querySelector(refocus);
      if (again) again.focus();
    }
    ui.toast(message);
    return true;
  }

  function notFound(message) {
    return '<div class="pagehead"><h1>Not found</h1><p>' + ui.esc(message || '') +
      '</p></div><a class="btn btn--primary" href="#/">Back to the home page</a>';
  }

  /* ============================================================= log modal */

  var draft = { rating: 0, status: 'finished' };

  /* ------------------------------------------------- instant rating strips */

  function paintStrip(strip, value) {
    var on = strip.querySelector('.rate__on');
    if (on) on.style.width = (Math.max(0, Math.min(5, value || 0)) / 5 * 100) + '%';
  }

  /* One game can have a strip in several places at once — a grid tile, the
     poster, a related-games row. Move all of them together. */
  function setStripValue(gameId, value) {
    Array.prototype.forEach.call(
      document.querySelectorAll('.rate[data-rate-for="' + gameId + '"]'),
      function (strip) {
        strip.dataset.rateValue = value;
        paintStrip(strip, value);
        var own = strip.closest('.tile__own');
        if (own) own.classList.remove('tile__own--blank');
      });
  }

  function openLogModal(gameId) {
    var me = store.currentUser();
    var game = PG.catalogue.resolve(gameId);
    if (!game) return;
    if (!me) {
      ui.toast('Sign in to log a game', true);
      go('#/signin');
      return;
    }

    var existing = store.myLog(gameId);
    draft.rating = existing && existing.rating ? existing.rating : 0;
    draft.status = store.statusOf(existing);
    var today = new Date().toISOString().slice(0, 10);

    var hits = '';
    for (var i = 1; i <= 10; i++) {
      var v = i / 2;
      hits += '<button type="button" data-act="rate" data-v="' + v +
        '" aria-label="' + v + ' stars">' + v + '</button>';
    }

    var stateButtons = store.states.map(function (s) {
      var on = draft.status === s;
      return '<button type="button" class="seg__btn' + (on ? ' is-on' : '') +
        '" data-act="set-status" data-status="' + s + '" role="radio" aria-checked="' +
        (on ? 'true' : 'false') + '">' + ui.esc(ui.stateLabel[s]) + '</button>';
    }).join('');

    var platformOptions = ['<option value="">Not set</option>'].concat(
      game.platforms.map(function (p) {
        return '<option value="' + ui.attr(p) + '"' +
          (existing && existing.platform === p ? ' selected' : '') + '>' +
          ui.esc(p) + '</option>';
      })).join('');

    ui.openModal(
      '<div class="modal__head">' +
        '<div style="width:44px;flex:none">' + ui.cover(game, { mini: true }) + '</div>' +
        '<div><div class="modal__title">' + ui.esc(game.title) + '</div>' +
          '<div class="modal__sub">' + ui.esc(game.year) + ' · ' +
          ui.esc(game.developer) + '</div></div>' +
        '<button class="modal__x" data-act="close-modal" aria-label="Close">✕</button>' +
      '</div>' +
      '<form id="log-form">' +
      '<div class="modal__body">' +
        '<div class="logrow">' +
          '<div><div class="logrow__label">Your rating</div>' +
            '<div class="rate" id="rate">' +
              '<span class="rate__track"><span class="rate__on" id="rate-on"></span></span>' +
              '<span class="rate__hit">' + hits + '</span>' +
            '</div></div>' +
          '<div><div class="logrow__label">Date played</div>' +
            '<input class="input" type="date" name="date" value="' +
              ui.attr(existing && existing.date ? existing.date : today) + '"></div>' +
          '<div><div class="logrow__label">Extras</div>' +
            '<div style="display:flex;gap:18px;flex-wrap:wrap">' +
            '<label class="check"><input type="checkbox" name="liked"' +
              (existing && existing.liked ? ' checked' : '') + '> Liked it</label>' +
            '<label class="check"><input type="checkbox" name="replay"' +
              (existing && existing.replay ? ' checked' : '') + '> Replay</label>' +
            '</div></div>' +
        '</div>' +

        '<div class="logrow">' +
          '<div><div class="logrow__label">Play state</div>' +
            '<div class="seg" role="radiogroup" aria-label="Play state">' +
              stateButtons + '</div>' +
            '<input type="hidden" name="status" value="' + ui.attr(draft.status) + '">' +
          '</div>' +
          '<div><div class="logrow__label">Hours played</div>' +
            '<input class="input input--num" type="number" name="hours" min="0" ' +
            'max="9999" step="0.5" inputmode="decimal" placeholder="e.g. 42" value="' +
            ui.attr(existing && existing.hours ? existing.hours : '') + '"></div>' +
          '<div><div class="logrow__label">Platform</div>' +
            '<select class="select" name="platform">' + platformOptions +
            '</select></div>' +
        '</div>' +
        '<label class="field"><span class="field__label">Review — optional</span>' +
          '<textarea class="textarea" name="review" maxlength="' + store.reviewMax +
            '" placeholder="What did you make of it?">' +
          ui.esc(existing ? existing.review : '') + '</textarea>' +
          /* Only worth showing once the review is long enough for the ceiling to
             be a real consideration — a counter on an empty box is clutter. */
          '<span class="charcount charcount--review' +
            ((existing && existing.review || '').length > store.reviewMax * 0.75 ?
              '' : ' is-quiet') + '">' +
            (existing && existing.review ? existing.review.length : 0) + ' / ' +
            store.reviewMax + '</span></label>' +
      '</div>' +
      '<div class="modal__foot">' +
        (existing ? '<button type="button" class="btn btn--danger btn--sm" data-act="delete-log" data-game="' +
          ui.attr(gameId) + '">Remove from diary</button>' : '') +
        '<button type="button" class="btn" data-act="close-modal">Cancel</button>' +
        '<button type="submit" class="btn btn--primary">' +
          (existing ? 'Save changes' : 'Add to diary') + '</button>' +
      '</div>' +
      '</form>'
    );

    document.getElementById('log-form').dataset.game = gameId;
    PG.art.hydrate(document.getElementById('modal'));
    paintRating(draft.rating);

    /* Hover preview across the ten half-star hit areas. */
    var hit = document.querySelector('.rate__hit');
    hit.addEventListener('mouseover', function (e) {
      var b = e.target.closest('button');
      if (b) paintRating(parseFloat(b.dataset.v));
    });
    hit.addEventListener('mouseleave', function () { paintRating(draft.rating); });
  }

  function paintRating(value) {
    var on = document.getElementById('rate-on');
    if (on) on.style.width = (Math.max(0, Math.min(5, value)) / 5 * 100) + '%';
  }

  function submitLog(form) {
    var gameId = form.dataset.game;
    if (!draft.rating) {
      ui.toast('Pick a star rating first', true);
      return;
    }
    var result = store.saveLog({
      game: gameId,
      rating: draft.rating,
      liked: form.liked.checked,
      replay: form.replay.checked,
      review: form.review.value.trim(),
      date: form.date.value,
      status: form.status.value,
      hours: form.hours.value,
      platform: form.platform.value
    });
    if (result.error) { ui.toast(result.error, true); return; }
    PG.sound.play('log');
    ui.closeModal();
    ui.toast('Logged ' + PG.catalogue.resolve(gameId).title);
    render();
  }

  /* ============================================================== rendering */

  function routeToHtml(route) {
    var p = route.parts;
    if (!p.length) return viewHome();
    switch (p[0]) {
      case 'games': return viewGames(route.query);
      case 'game': return viewGame(p[1]);
      case 'members': return viewMembers(route.query);
      case 'member':
        if (!p[2]) return viewMember(p[1]);
        /* /followers and /following are people; everything else is a shelf. */
        if (p[2] === 'followers' || p[2] === 'following') {
          return viewFollowList(p[1], p[2]);
        }
        return viewMemberList(p[1], p[2]);
      case 'activity': return viewActivity(route.query);
      case 'messages': return p[1] ? viewThread(p[1]) : viewMessages();
      case 'settings': return viewSettings();
      case 'signin': return viewAuth();
      default: return notFound('That page does not exist.');
    }
  }

  var lastKey = '';
  var renderTicket = 0;

  /* Grey boxes in roughly the shape of what is coming. Shown only after a
     delay (see render), so a fast backend never flashes them. */
  function skeletonFor(route) {
    var top = route.parts[0] || 'home';
    var tiles = '';
    for (var i = 0; i < 12; i++) tiles += '<div class="skel__tile"></div>';

    if (top === 'game') {
      return '<div class="skel" aria-busy="true" aria-live="polite">' +
        '<span class="skel__sr">Loading…</span>' +
        '<div class="skel__hero"></div>' +
        '<div class="skel__line skel__line--wide"></div>' +
        '<div class="skel__line"></div></div>';
    }
    return '<div class="skel" aria-busy="true" aria-live="polite">' +
      '<span class="skel__sr">Loading…</span>' +
      '<div class="skel__line skel__line--wide"></div>' +
      '<div class="skel__grid">' + tiles + '</div></div>';
  }

  /* What a reader sees when a read fails. The distinction from `.empty`
     matters: "no games match those filters" and "we could not ask" look
     identical if both render as an empty grid, and only one of them is the
     reader's fault. */
  function failurePanel(err) {
    var kind = err && err.kind;
    var line = kind === 'offline' ?
        'Could not reach Postgame. Check your connection.' :
      kind === 'timeout' ?
        'That took too long to load.' :
      err && err.message ? String(err.message) :
        'Something went wrong loading this page.';

    return '<div class="empty empty--failed" role="alert">' +
      '<div class="empty__t">This did not load</div>' +
      '<div>' + ui.esc(line) + '</div>' +
      '<div class="empty__actions">' +
        '<button class="btn btn--primary" data-act="retry">Try again</button>' +
      '</div></div>';
  }

  function render(keepScroll) {
    var route = parseHash();

    /* Reviews folded into Activity; old links and bookmarks still work. */
    if (route.parts[0] === 'reviews') {
      window.location.replace('#/activity' + toQuery({ show: 'reviews' }));
      return;
    }

    /* The query is part of the identity of a page: changing a filter should
       start its list again, while "show more" (which leaves the hash alone)
       should not. */
    var key = route.parts.join('/') + toQuery(route.query);
    if (key !== lastKey) {
      state.gamesPage = 1;
      state.feedPage = 1;
      /* A validation message belongs to the visit that caused it, not to the
         next time settings is opened. */
      if (route.parts[0] !== 'settings') state.settingsError = '';
      lastKey = key;
    }
    /* Re-rendering replaces the filter controls, so put the caret back where
       the member left it — otherwise typing in the browse box loses focus. */
    var active = document.activeElement;
    var restore = active && active.dataset && active.dataset.filter ?
      { name: active.dataset.filter, pos: active.selectionStart } : null;

    /* Views became async in Phase 4, so a navigation can now finish after a
       later one started. Every render takes a ticket and only the newest one
       is allowed to write — otherwise clicking through three games quickly
       leaves whichever request happened to be slowest on screen. */
    var ticket = ++renderTicket;

    /* Only show a skeleton when there is nothing useful on screen already.
       Replacing a rendered page with grey boxes for 30ms is worse than a brief
       pause, and on the local backend the promise resolves in the same tick. */
    var placeholder = window.setTimeout(function () {
      if (ticket === renderTicket) view.innerHTML = skeletonFor(route);
    }, 120);

    function settle(html) {
      window.clearTimeout(placeholder);
      if (ticket !== renderTicket) return;

      view.innerHTML = html;
      /* Before paintChrome, so the header's unread badge reflects this visit. */
      markVisited(route);
      paintChrome(route);
      PG.art.hydrate(view);

      if (restore) {
        var again = view.querySelector('[data-filter="' + restore.name + '"]');
        if (again) {
          again.focus();
          if (again.setSelectionRange && restore.pos !== null) {
            try { again.setSelectionRange(restore.pos, restore.pos); } catch (e) { /* type=date etc. */ }
          }
        }
      }
      if (!keepScroll) window.scrollTo(0, 0);
      /* A thread opens at the newest message, the way every chat app does. */
      if (route.parts[0] === 'messages' && route.parts[1]) scrollThreadToEnd();
    }

    Promise.resolve()
      .then(function () { return routeToHtml(route); })
      .then(settle, function (err) {
        /* A failed read used to render as an empty shelf, which tells the
           reader the catalogue is empty rather than that we could not ask. */
        settle(failurePanel(err));
        if (window.console) console.error('Postgame: view failed', err);
      });
  }

  /* State that changes *because a route was visited*, rather than because
     something was clicked. It lives here and not in a view builder so that
     producing markup never writes — a re-render triggered for any other reason
     must not quietly mutate stored data. */
  function markVisited(route) {
    if (route.parts[0] !== 'messages' || !route.parts[1]) return;
    /* Opening a conversation is reading it. */
    if (store.currentUser() && store.findMember(route.parts[1])) {
      store.markRead(route.parts[1]);
    }
  }

  function paintChrome(route) {
    var top = route.parts[0] || 'home';
    Array.prototype.forEach.call(document.querySelectorAll('.nav a'), function (a) {
      var target = a.dataset.route;
      a.classList.toggle('is-active',
        target === top || (target === 'games' && top === 'game') ||
        (target === 'members' && top === 'member'));
    });

    var me = store.currentUser();
    var host = document.getElementById('account');
    if (!me) {
      host.innerHTML = '<a class="btn btn--primary btn--sm" href="#/signin">Sign in</a>';
      return;
    }
    var unread = store.unreadTotal();
    host.innerHTML =
      '<button class="accountbtn" data-act="menu" aria-haspopup="true" aria-expanded="false"' +
        (unread ? ' aria-label="Account menu, ' + unread + ' unread messages"' : '') + '>' +
        ui.avatar(me) + '<span class="accountbtn__name">' + ui.esc(me.name) +
        '</span>' +
        (unread ? '<span class="accountbtn__dot" aria-hidden="true"></span>' : '') +
        '<span class="accountbtn__caret">▾</span></button>' +
      '<div class="menu" id="usermenu">' +
        '<div class="menu__label">@' + ui.esc(me.username) + '</div>' +
        '<a href="#/member/' + ui.attr(me.username) + '">Profile</a>' +
        '<a href="#/messages">Messages' +
          (unread ? '<span class="menu__count">' + ui.fmtNumber(unread) + '</span>' : '') +
        '</a>' +
        '<a href="#/settings">Settings</a>' +
        '<div class="menu__sep"></div>' +
        '<button data-act="signout">Sign out</button>' +
      '</div>';
  }

  /* =============================================================== search */

  var searchState = { cursor: -1, results: [] };

  function initSearch() {
    var box = document.getElementById('search');
    var input = document.getElementById('search-input');
    var out = document.getElementById('search-results');

    function close() {
      box.classList.remove('is-open');
      searchState.cursor = -1;
    }

    /* Every keystroke used to filter an in-memory array; now it may be a
       request. Two consequences handled here: the answers can arrive out of
       order, so a stale one must not overwrite a newer one, and the endpoint is
       the most tightly rate-limited in the app, so the typing is debounced
       rather than fired per character. */
    var suggestTicket = 0;
    var suggestTimer = null;

    function paint() {
      var q = input.value.trim();
      box.classList.toggle('is-filled', !!q);
      if (q.length < 2) { close(); return; }

      window.clearTimeout(suggestTimer);
      suggestTimer = window.setTimeout(function () { ask(q); }, 140);
    }

    function ask(q) {
      var ticket = ++suggestTicket;
      PG.catalogue.suggest(q).then(function (hits) {
        if (ticket !== suggestTicket) return;
        show(hits.slice(0, 6), q);
      }, function (err) {
        if (ticket !== suggestTicket) return;
        /* A failed *request* is not worth alarming anyone over — the dropdown is
           a convenience, and the page still works without it. But a bug in our
           own rendering lands here too, and swallowing that hid a real one
           during Phase 4: `show()` referenced a variable that was no longer in
           scope, threw, and looked exactly like the search being offline. */
        if (!(err instanceof PG.api.ApiError) && window.console) {
          console.error('Postgame: search dropdown failed', err);
        }
        close();
      });
    }

    function show(hits, q) {
      searchState.results = hits;
      searchState.cursor = -1;
      if (!hits.length) {
        out.innerHTML = '<div class="sresult"><div class="sresult__meta">' +
          '<div class="sresult__t">No games found</div>' +
          '<div class="sresult__s">Try another title or studio</div></div></div>';
      } else {
        out.innerHTML = hits.map(function (g, i) {
          return '<a class="sresult" href="#/game/' + ui.attr(g.id) + '" data-i="' + i + '">' +
            '<span class="sresult__cover">' + ui.cover(g, { mini: true }) + '</span>' +
            '<span class="sresult__meta"><div class="sresult__t">' + ui.esc(g.title) +
            '</div><div class="sresult__s">' + ui.esc(g.year) + ' · ' +
            ui.esc(g.developer) + '</div></span></a>';
        }).join('') +
        '<a class="sresult__all" href="#/games' + toQuery({ q: q }) + '">See all results for “' +
          ui.esc(q) + '”</a>';
      }
      PG.art.hydrate(out);
      box.classList.add('is-open');
    }

    input.addEventListener('input', paint);
    input.addEventListener('focus', function () { if (input.value.trim().length > 1) paint(); });

    input.addEventListener('keydown', function (e) {
      var items = out.querySelectorAll('.sresult[data-i]');
      if (e.key === 'Escape') { close(); input.blur(); return; }
      if (e.key === 'Enter') {
        if (searchState.cursor > -1 && items[searchState.cursor]) {
          e.preventDefault();
          go('#/game/' + searchState.results[searchState.cursor].id);
          input.value = '';
          box.classList.remove('is-filled');
          close();
          input.blur();
        } else if (input.value.trim()) {
          e.preventDefault();
          go('#/games' + toQuery({ q: input.value.trim() }));
          close();
          input.blur();
        }
        return;
      }
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
      e.preventDefault();
      if (!items.length) return;
      searchState.cursor += e.key === 'ArrowDown' ? 1 : -1;
      if (searchState.cursor < 0) searchState.cursor = items.length - 1;
      if (searchState.cursor >= items.length) searchState.cursor = 0;
      Array.prototype.forEach.call(items, function (el, i) {
        el.classList.toggle('is-cursor', i === searchState.cursor);
      });
    });

    document.getElementById('search-clear').addEventListener('click', function () {
      input.value = '';
      box.classList.remove('is-filled');
      close();
      input.focus();
    });

    out.addEventListener('click', function () {
      window.setTimeout(function () {
        input.value = '';
        box.classList.remove('is-filled');
        close();
      }, 0);
    });

    document.addEventListener('click', function (e) {
      if (!box.contains(e.target)) close();
    });
  }

  /* ============================================================== actions */

  function closeMenus() {
    Array.prototype.forEach.call(document.querySelectorAll('.menu.is-open'), function (m) {
      m.classList.remove('is-open');
    });
  }

  function onClick(e) {
    var hit = e.target.closest('[data-act]');
    if (!hit) { closeMenus(); return; }
    var act = hit.dataset.act;

    if (act !== 'menu') closeMenus();

    switch (act) {
      case 'menu': {
        var menu = document.getElementById('usermenu');
        var open = menu.classList.toggle('is-open');
        hit.setAttribute('aria-expanded', open ? 'true' : 'false');
        break;
      }
      case 'log':
        openLogModal(hit.dataset.game);
        break;
      case 'follow': {
        var who = hit.dataset.user;
        var res = store.toggleFollow(who);
        if (res.error) { ui.toast(res.error, true); go('#/signin'); break; }
        PG.sound.play(res.following ? 'follow' : 'unfollow');
        render(true);
        /* The button was replaced by the re-render — keep the keyboard on it so
           following several members in a row does not need the mouse. */
        var same = document.querySelector(
          '[data-act="follow"][data-user="' + cssQuote(who) + '"]');
        if (same) same.focus();
        var them = store.findMember(who);
        ui.toast(res.following ? 'Following ' + them.name :
          'Unfollowed ' + them.name);
        break;
      }
      case 'want': {
        var id = hit.dataset.game;
        var next = !store.inBacklog(id);
        var res = store.setBacklog(id, next);
        if (res.error) { ui.toast(res.error, true); go('#/signin'); break; }
        ui.toast(next ? 'Added to your want to play list' : 'Removed from your list');
        render(true);
        break;
      }
      case 'rate':
        draft.rating = parseFloat(hit.dataset.v);
        paintRating(draft.rating);
        break;
      case 'set-status': {
        draft.status = hit.dataset.status;
        var panel = document.getElementById('modal');
        Array.prototype.forEach.call(
          panel.querySelectorAll('[data-act="set-status"]'), function (b) {
            var on = b.dataset.status === draft.status;
            b.classList.toggle('is-on', on);
            b.setAttribute('aria-checked', on ? 'true' : 'false');
          });
        var hidden = panel.querySelector('input[name="status"]');
        if (hidden) hidden.value = draft.status;
        break;
      }
      case 'quick-rate': {
        var rateId = hit.dataset.game;
        var value = parseFloat(hit.dataset.v);
        var rated = store.quickRate(rateId, value);
        if (rated.error) { ui.toast(rated.error, true); go('#/signin'); break; }
        setStripValue(rateId, value);
        ui.toast('Rated ' + PG.catalogue.resolve(rateId).title + ' — ' + value + '★');
        /* The game page shows the average and histogram this just moved. */
        if (parseHash().parts[0] === 'game') render(true);
        break;
      }
      case 'quick-state': {
        var stateId = hit.dataset.game;
        var moved = store.quickState(stateId, hit.dataset.status);
        if (moved.error) { ui.toast(moved.error, true); go('#/signin'); break; }
        ui.toast(ui.stateLabel[hit.dataset.status] + ' — ' + PG.catalogue.resolve(stateId).title);
        render(true);
        break;
      }
      case 'new-message':
        openNewMessage();
        break;
      case 'close-modal':
        ui.closeModal();
        break;
      case 'delete-log': {
        store.removeLog(hit.dataset.game);
        ui.closeModal();
        ui.toast('Removed from your diary');
        render();
        break;
      }
      case 'like-review': {
        var r = store.toggleReviewLike(hit.dataset.id);
        if (r.error) { ui.toast(r.error, true); break; }
        render(true);
        break;
      }
      case 'toggle-thread': {
        var threadId = hit.dataset.id;
        var opening = !state.threads[threadId];
        if (opening) state.threads[threadId] = 1;
        else delete state.threads[threadId];
        render(true);
        /* Keep the button under the cursor rather than dumping focus on body,
           and put the caret straight in the box when opening. */
        var box = opening && document.querySelector(
          '.cform[data-log="' + cssQuote(threadId) + '"] textarea');
        if (box) box.focus();
        else focusToggle(threadId);
        break;
      }
      case 'reply-comment': {
        var replyTo = hit.dataset.log;
        var handle = '@' + hit.dataset.user + ' ';
        var field = document.querySelector(
          '.cform[data-log="' + cssQuote(replyTo) + '"] textarea');
        if (!field) { ui.toast('Sign in to reply', true); go('#/signin'); break; }
        /* Append rather than replace: replying to a second person mid-draft
           should not throw away what is already typed. */
        if (field.value.indexOf(handle.trim()) === -1) {
          field.value = field.value ? field.value.replace(/\s*$/, ' ') + handle : handle;
        }
        field.focus();
        field.setSelectionRange(field.value.length, field.value.length);
        paintCommentCount(field);
        break;
      }
      case 'delete-comment': {
        var gone = store.removeComment(hit.dataset.id);
        if (gone.error) { ui.toast(gone.error, true); break; }
        render(true);
        ui.toast('Comment deleted');
        break;
      }
      case 'more-games':
        state.gamesPage++;
        render(true);
        break;
      /* Offered by failurePanel(). Re-running the same render is the whole
         retry: nothing was written, so there is no half-finished state to
         undo — the read simply happens again. */
      case 'retry':
        render(true);
        break;
      case 'more-feed':
        state.feedPage++;
        render(true);
        break;
      case 'auth-tab':
        state.authTab = hit.dataset.tab;
        state.authError = '';
        render(true);
        break;
      case 'set-theme': {
        var picked = hit.dataset.theme;
        PG.theme.set(picked);
        render(true);
        var seg = document.querySelector('[data-act="set-theme"][data-theme="' +
          picked + '"]');
        if (seg) seg.focus();
        ui.toast('Theme: ' + PG.theme.labels[PG.theme.get()]);
        break;
      }
      case 'set-sound': {
        var wanted = hit.dataset.on === '1';
        PG.sound.set(wanted);
        render(true);
        var seg = document.querySelector(
          '[data-act="set-sound"][data-on="' + (wanted ? '1' : '0') + '"]');
        if (seg) seg.focus();
        ui.toast('Sound effects ' + (wanted ? 'on' : 'off'));
        break;
      }
      case 'set-hue': {
        var huePatch = profileFormValues();
        huePatch.hue = hit.dataset.hue;
        saveProfile(huePatch, 'Avatar colour updated',
          '[data-act="set-hue"][data-hue="' + hit.dataset.hue + '"]');
        break;
      }
      case 'drop-avatar': {
        var dropPatch = profileFormValues();
        dropPatch.avatar = '';
        saveProfile(dropPatch, 'Picture removed', '#avatar-input');
        break;
      }
      case 'demo': {
        var out = store.login(hit.dataset.user, store.demoPassword);
        if (out.error) { ui.toast(out.error, true); break; }
        ui.toast('Signed in as ' + out.user.name);
        go('#/member/' + out.user.username);
        break;
      }
      case 'signout':
        store.logout();
        ui.toast('Signed out');
        go('#/');
        render();
        break;
    }
  }

  function onSubmit(e) {
    var form = e.target;
    if (form.id === 'log-form') {
      e.preventDefault();
      submitLog(form);
      return;
    }
    if (form.id === 'signin-form') {
      e.preventDefault();
      var res = store.login(form.username.value.trim(), form.password.value);
      if (res.error) { state.authError = res.error; render(true); return; }
      state.authError = '';
      ui.toast('Welcome back, ' + res.user.name);
      go('#/member/' + res.user.username);
      return;
    }
    if (form.id === 'signup-form') {
      e.preventDefault();
      var out = store.register(form.username.value.trim(), form.name.value.trim(),
        form.password.value);
      if (out.error) { state.authError = out.error; render(true); return; }
      state.authError = '';
      ui.toast('Account created. Welcome to Postgame.');
      go('#/member/' + out.user.username);
      return;
    }
    if (form.id === 'profile-form') {
      e.preventDefault();
      saveProfile({ name: form.name.value, bio: form.bio.value }, 'Profile saved');
      return;
    }
    if (form.classList.contains('mform')) {
      e.preventDefault();
      var to = form.dataset.to;
      var sent = store.sendMessage(to, form.body.value);
      if (sent.error) { ui.toast(sent.error, true); return; }
      render(true);
      var box = document.querySelector('.mform textarea');
      if (box) box.focus();
      scrollThreadToEnd();
      return;
    }
    if (form.classList.contains('cform')) {
      e.preventDefault();
      var logId = form.dataset.log;
      var posted = store.addComment(logId, form.body.value);
      if (posted.error) { ui.toast(posted.error, true); return; }
      /* Leave the thread open so the new comment is visible where it landed. */
      state.threads[logId] = 1;
      render(true);
      var again = document.querySelector(
        '.cform[data-log="' + cssQuote(logId) + '"] textarea');
      if (again) again.focus();
      ui.toast('Comment posted');
      return;
    }
  }

  /* Filters write to the hash so the view is shareable and the back button
     behaves. Route-aware, so the same inputs drive the games browser and the
     member search without either knowing about the other. */
  function onFilterChange(e) {
    var el = e.target.closest('[data-filter]');
    if (!el) return;
    var route = parseHash();
    var base = '#/' + (route.parts[0] || 'games');
    var q = route.query;
    q[el.dataset.filter] = el.value;
    var next = base + toQuery(q);
    if (next === window.location.hash) render();
    else go(next);
  }

  /* ================================================================= boot */

  function boot() {
    view = document.getElementById('view');
    initSearch();
    document.addEventListener('click', onClick);
    document.addEventListener('submit', onSubmit);
    document.addEventListener('change', onFilterChange);

    /* The title/studio box on the browse page filters as you type. */
    var typingTimer = null;
    document.addEventListener('input', function (e) {
      /* The recipient picker filters instantly — it is a short local list, so
         there is nothing to debounce. */
      if (e.target.id === 'pick-search') { filterRecipients(e.target.value); return; }
      var el = e.target.closest('input[data-filter]');
      if (el) {
        window.clearTimeout(typingTimer);
        typingTimer = window.setTimeout(function () { onFilterChange(e); }, 260);
        return;
      }
      /* Bio length, updated as you type rather than only on save. */
      if (e.target.name === 'bio') {
        var count = document.querySelector('#profile-form .charcount');
        if (count) {
          count.textContent = e.target.value.length + ' / ' + store.bioMax;
        }
        return;
      }
      /* Review length. Stays out of the way until you are near the ceiling. */
      if (e.target.name === 'review') {
        var rc = document.querySelector('.charcount--review');
        if (rc) {
          rc.textContent = e.target.value.length + ' / ' + store.reviewMax;
          rc.classList.toggle('is-quiet', e.target.value.length <= store.reviewMax * 0.75);
        }
        return;
      }
      if (e.target.closest('.cform')) paintCommentCount(e.target);
      var mform = e.target.closest('.mform');
      if (mform) {
        var mc = mform.querySelector('.charcount');
        if (mc) mc.textContent = e.target.value.length + ' / ' + store.messageMax;
      }
    });

    /* A file input fires change, never click, so it cannot go through onClick. */
    document.addEventListener('change', function (e) {
      if (e.target.id !== 'avatar-input') return;
      var input = e.target;
      var file = input.files && input.files[0];
      if (!file) return;
      readAvatar(file, function (result) {
        /* Clear it either way, so choosing the same file again still fires. */
        input.value = '';
        if (result.error) {
          state.settingsError = result.error;
          render(true);
          return;
        }
        var patch = profileFormValues();
        patch.avatar = result.url;
        saveProfile(patch, 'Picture updated', '#avatar-input');
      });
    });

    /* Preview the score under the cursor on any instant-rating strip, and put
       it back when the pointer leaves. */
    document.addEventListener('mouseover', function (e) {
      var star = e.target.closest && e.target.closest('[data-act="quick-rate"]');
      if (!star) return;
      var strip = star.closest('.rate');
      if (strip) paintStrip(strip, parseFloat(star.dataset.v));
    });

    document.addEventListener('mouseout', function (e) {
      var strip = e.target.closest && e.target.closest('.rate[data-rate-for]');
      if (!strip || strip.contains(e.relatedTarget)) return;
      paintStrip(strip, parseFloat(strip.dataset.rateValue || 0));
    });

    /* The router owns the hash, so following href="#view" would be read as a
       route. Move focus directly instead and leave the URL alone. */
    var skip = document.querySelector('.skip');
    if (skip) {
      skip.addEventListener('click', function (e) {
        e.preventDefault();
        var target = document.getElementById('view');
        if (!target) return;
        target.focus();
        target.scrollIntoView();
      });
    }

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        if (ui.modalIsOpen()) ui.closeModal();
        closeMenus();
      }
      /* "/" focuses search, the way every good catalogue does it. */
      if (e.key === '/' && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
        e.preventDefault();
        document.getElementById('search-input').focus();
      }
      /* Ctrl/Cmd+Enter posts a comment, since plain Enter has to stay available
         for paragraph breaks in the box. */
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        var box = e.target.closest && e.target.closest('.cform, .mform');
        if (box) {
          e.preventDefault();
          box.dispatchEvent(new window.Event('submit', { bubbles: true, cancelable: true }));
        }
      }
    });

    document.getElementById('modal').addEventListener('click', function (e) {
      if (e.target.id === 'modal') ui.closeModal();
    });

    window.addEventListener('hashchange', function () {
      if (ui.modalIsOpen()) ui.closeModal();
      render();
    });

    if (!window.location.hash) window.location.replace('#/');
    render();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})(window.PG);
