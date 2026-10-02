/* Postgame — cover art
   Real box art, pulled lazily from Wikipedia's public API and cached in
   localStorage. Nothing here is required for the site to work: if the network
   is unavailable, an article has no image, or the page is opened straight from
   the filesystem with fetch blocked, the generated cover underneath simply
   stays put.

   Two passes per game:
     1. an exact title lookup, batched 30 at a time
     2. for anything that missed, a one-shot search of the same title

   Only game titles ever leave the browser, and only to en.wikipedia.org. */
window.PG = window.PG || {};
(function (PG) {
  'use strict';

  /* v3 retires entries cached before the film filter existed — Minecraft had
     picked up "A Minecraft Movie poster.jpg" and kept serving it from storage
     long after the resolver stopped choosing it. */
  var CACHE_KEY = 'postgame.art.v3';
  var ENDPOINT = 'https://en.wikipedia.org/w/api.php';
  var BATCH_SIZE = 30;
  var MAX_SEARCHES = 3;
  var THUMB = 600;
  var EAGER_LIMIT = 24;
  var FRAME = 3 / 4;        /* the cover frame's aspect ratio */
  /* How much of an image may be trimmed to fill the frame. A third off one
     dimension is what every storefront does to key art and is far better than
     a letterboxed sliver; a wordmark gets almost no budget, since clipping the
     ends off "MINECRAFT" reads as broken rather than as a crop. */
  var MAX_CROP = 0.35;
  var WORDMARK_CROP = 0.1;

  var art = {};            /* gameId -> image url, or 0 when nothing was found */
  var waiting = {};        /* gameId -> [elements awaiting art] */
  var lookupQueue = [];
  var searchQueue = [];
  var searching = 0;
  var batchTimer = null;
  var saveTimer = null;
  var enabled = true;
  var failures = 0;

  /* Articles whose title does not match ours — disambiguated pages, accents,
     remakes that share a name with the original. Anything not listed here
     falls through to the search pass. */
  var ARTICLES = {
    'doom-2016': 'Doom (2016 video game)',
    'doom-1993': 'Doom (1993 video game)',
    'doom-eternal': 'Doom Eternal',
    'portal': 'Portal (video game)',
    'half-life': 'Half-Life (video game)',
    'quake': 'Quake (video game)',
    'contra': 'Contra (video game)',
    'prey': 'Prey (2017 video game)',
    'control': 'Control (video game)',
    'inside': 'Inside (video game)',
    'limbo': 'Limbo (video game)',
    'journey': 'Journey (2012 video game)',
    'flower': 'Flower (video game)',
    'braid': 'Braid (video game)',
    'celeste': 'Celeste (video game)',
    'hades': 'Hades (video game)',
    'tunic': 'Tunic (video game)',
    'stray': 'Stray (video game)',
    'fable': 'Fable (2004 video game)',
    'bastion': 'Bastion (video game)',
    'transistor': 'Transistor (video game)',
    'pyre': 'Pyre (video game)',
    'catherine': 'Catherine (video game)',
    'bayonetta': 'Bayonetta (video game)',
    'starfield': 'Starfield (video game)',
    'stellaris': 'Stellaris (video game)',
    'rust': 'Rust (video game)',
    'the-forest': 'The Forest (video game)',
    'dayz': 'DayZ (video game)',
    'satisfactory': 'Satisfactory (video game)',
    'new-world': 'New World (video game)',
    'blasphemous': 'Blasphemous (video game)',
    'sleeping-dogs': 'Sleeping Dogs (video game)',
    'bully': 'Bully (video game)',
    'max-payne': 'Max Payne (video game)',
    'the-sims': 'The Sims (video game)',
    'starcraft': 'StarCraft (video game)',
    'black-and-white': 'Black & White (video game)',
    'mass-effect': 'Mass Effect (video game)',
    'destiny': 'Destiny (video game)',
    'overwatch': 'Overwatch (video game)',
    'deus-ex': 'Deus Ex (video game)',
    'mega-man-x': 'Mega Man X (video game)',
    'ico': 'Ico (video game)',
    'shenmue': 'Shenmue (video game)',
    'rock-band': 'Rock Band (video game)',
    'the-witness': 'The Witness (2016 video game)',
    'full-throttle': 'Full Throttle (1995 video game)',
    'the-walking-dead': 'The Walking Dead (video game)',
    'life-is-strange': 'Life Is Strange (2015 video game)',
    'steins-gate': 'Steins;Gate (video game)',
    'doki-doki-literature-club': 'Doki Doki Literature Club!',
    'monument-valley': 'Monument Valley (video game)',
    'angry-birds': 'Angry Birds (video game)',
    'garena-free-fire': 'Free Fire (video game)',
    'five-nights-at-freddy-s': "Five Nights at Freddy's (video game)",
    'nba-jam': 'NBA Jam (1993 video game)',
    'madden-nfl-25': 'Madden NFL 25 (2024 video game)',
    'need-for-speed-most-wanted': 'Need for Speed: Most Wanted (2005 video game)',
    'goldeneye-007': 'GoldenEye 007 (1997 video game)',
    'perfect-dark': 'Perfect Dark (2000 video game)',
    'donkey-kong': 'Donkey Kong (1981 video game)',
    'mortal-kombat': 'Mortal Kombat (1992 video game)',
    'mortal-kombat-1': 'Mortal Kombat 1 (2023 video game)',
    'street-fighter-ii': 'Street Fighter II: The World Warrior',
    'marvel-vs-capcom-2': 'Marvel vs. Capcom 2: New Age of Heroes',
    'dance-dance-revolution': 'Dance Dance Revolution (1998 video game)',
    'microsoft-flight-simulator': 'Microsoft Flight Simulator (2020 video game)',
    'brain-age': 'Brain Age: Train Your Brain in Minutes a Day!',
    'okami': 'Okami',
    'god-of-war': 'God of War (2018 video game)',
    'god-of-war-ragnarok': 'God of War Ragnarok',
    'marvel-s-spider-man': "Marvel's Spider-Man (2018 video game)",
    'demon-s-souls': "Demon's Souls (2020 video game)",
    'dead-space': 'Dead Space (2008 video game)',
    'dead-space-remake': 'Dead Space (2023 video game)',
    'resident-evil-4': 'Resident Evil 4',
    'resident-evil-4-remake': 'Resident Evil 4 (2023 video game)',
    'resident-evil-2': 'Resident Evil 2',
    'resident-evil-2-remake': 'Resident Evil 2 (2019 video game)',
    'silent-hill-2-remake': 'Silent Hill 2 (2024 video game)',
    'nier-replicant-ver-1-22474487139': 'Nier Replicant',
    'dragon-s-dogma-dark-arisen': "Dragon's Dogma",
    'monster-hunter-4-ultimate': 'Monster Hunter 4',
    'xenoblade-chronicles': 'Xenoblade Chronicles (video game)',
    'star-wars-knights-of-the-old-republic': 'Star Wars: Knights of the Old Republic (video game)',
    'star-wars-battlefront-ii': 'Star Wars Battlefront II (2017 video game)',
    'viva-pinata': 'Viva Pinata',
    'mario-kart-8-deluxe': 'Mario Kart 8',
    'sonic-the-hedgehog-2': 'Sonic the Hedgehog 2',
    'pokemon-go': 'Pokemon Go',
    'darkstalkers': 'Darkstalkers: The Night Warriors',
    'tom-clancy-s-splinter-cell': "Tom Clancy's Splinter Cell (video game)",
    'god-of-war-2005': 'God of War (2005 video game)',
    'tomb-raider-2013': 'Tomb Raider (2013 video game)',
    'hitman-2016': 'Hitman (2016 video game)',
    'call-of-duty-modern-warfare-2019': 'Call of Duty: Modern Warfare (2019 video game)',
    'call-of-duty-modern-warfare-ii-2022': 'Call of Duty: Modern Warfare II',
    'star-wars-battlefront-ii-2005': 'Star Wars: Battlefront II (2005 video game)',
    'serious-sam-the-first-encounter': 'Serious Sam: The First Encounter',
    'the-king-of-fighters-98': "The King of Fighters '98: The Slugfest",
    'metroid-prime-4-beyond': 'Metroid Prime 4: Beyond',
    'warioware-inc-mega-microgames': 'WarioWare, Inc.: Mega Microgame$!',
    'super-mario-world-2-yoshi-s-island': "Super Mario World 2: Yoshi's Island",
    'nine-hours-nine-persons-nine-doors': 'Nine Hours, Nine Persons, Nine Doors',
    'vampire-the-masquerade-bloodlines': 'Vampire: The Masquerade – Bloodlines',
    'commandos-2-men-of-courage': 'Commandos 2: Men of Courage',
    'ms-pac-man': 'Ms. Pac-Man',
    'r-type': 'R-Type',
    's-t-a-l-k-e-r-shadow-of-chernobyl': 'S.T.A.L.K.E.R.: Shadow of Chernobyl',
    's-t-a-l-k-e-r-2-heart-of-chornobyl': 'S.T.A.L.K.E.R. 2: Heart of Chornobyl',
    'f-e-a-r': 'F.E.A.R.',
    'invisible-inc': 'Invisible, Inc.',
    'ultrakill': 'Ultrakill',
    'va-11-hall-a': 'VA-11 Hall-A',
    'abzu': 'Abzu (video game)',
    'gris': 'Gris (video game)',
    'fez': 'Fez (video game)',
    'noita': 'Noita (video game)',
    'norco': 'Norco (video game)',
    'signalis': 'Signalis',
    'soma': 'Soma (video game)',
    'pong': 'Pong',
    'metroid': 'Metroid (video game)',
    'castlevania': 'Castlevania (1986 video game)',
    'diablo': 'Diablo (video game)',
    'fallout': 'Fallout (video game)',
    'gothic': 'Gothic (video game)',
    'unreal': 'Unreal (1998 video game)',
    'crysis': 'Crysis (video game)',
    'far-cry': 'Far Cry (video game)',
    'borderlands': 'Borderlands (video game)',
    'titanfall': 'Titanfall (video game)',
    'homeworld': 'Homeworld (video game)',
    'northgard': 'Northgard',
    'frostpunk': 'Frostpunk',
    'prototype': 'Prototype (video game)',
    'clannad': 'Clannad (visual novel)',
    'syberia': 'Syberia (video game)',
    'strider': 'Strider (1989 video game)',
    'contra': 'Contra (video game)',
    'galaga': 'Galaga',
    'asteroids': 'Asteroids (video game)',
    'guitar-hero': 'Guitar Hero (video game)',
    'final-fantasy': 'Final Fantasy (video game)',
    'dragon-quest': 'Dragon Quest (video game)',
    'kingdom-hearts': 'Kingdom Hearts (video game)',
    'tomb-raider': 'Tomb Raider (1996 video game)',
    'resident-evil': 'Resident Evil (1996 video game)',
    'silent-hill': 'Silent Hill (video game)',
    'devil-may-cry': 'Devil May Cry (video game)',
    'metal-gear': 'Metal Gear (video game)',
    'age-of-empires': 'Age of Empires (video game)',
    'command-and-conquer': 'Command & Conquer (1995 video game)',
    'the-witcher': 'The Witcher (video game)',
    'baldur-s-gate': "Baldur's Gate (video game)",
    'crash-bandicoot': 'Crash Bandicoot (video game)',
    'the-legend-of-zelda': 'The Legend of Zelda (video game)',
    'splatoon': 'Splatoon (video game)',
    'animal-crossing': 'Animal Crossing (video game)',
    'pikmin': 'Pikmin (video game)',
    'star-fox': 'Star Fox (video game)',
    'f-zero': 'F-Zero (video game)',
    'nethack': 'NetHack',
    'oxenfree': 'Oxenfree'
  };

  /* ------------------------------------------------------------------ cache */

  try {
    art = JSON.parse(window.localStorage.getItem(CACHE_KEY)) || {};
  } catch (e) {
    art = {};
  }

  function persist() {
    window.clearTimeout(saveTimer);
    saveTimer = window.setTimeout(function () {
      try {
        window.localStorage.setItem(CACHE_KEY, JSON.stringify(art));
      } catch (e) { /* quota — the in-memory cache still works this session */ }
    }, 600);
  }

  /* ------------------------------------------------------------- requesting */

  function articleFor(id) {
    var game = PG.data.byId[id];
    return ARTICLES[id] || (game ? game.title : null);
  }

  function raw(params) {
    return window.fetch(ENDPOINT + '?' + params.join('&'), {
      credentials: 'omit',
      referrerPolicy: 'origin-when-cross-origin'
    }).then(function (r) {
      if (!r.ok) throw new Error('http ' + r.status);
      return r.json();
    });
  }

  function api(params) {
    return raw(['action=query', 'format=json', 'formatversion=2', 'origin=*',
      'redirects=1', 'prop=pageimages', 'piprop=thumbnail',
      'pithumbsize=' + THUMB, 'pilicense=any'].concat(params));
  }

  function scheduleBatch() {
    if (batchTimer) return;
    batchTimer = window.setTimeout(function () {
      batchTimer = null;
      runBatch();
    }, 90);
  }

  function runBatch() {
    if (!enabled || !lookupQueue.length) return;
    var ids = lookupQueue.splice(0, BATCH_SIZE);
    var titles = [];
    var byArticle = {};
    ids.forEach(function (id) {
      var title = articleFor(id);
      if (!title) return;
      /* Two games can point at one article; keep every claimant. */
      (byArticle[title] || (byArticle[title] = [])).push(id);
      if (titles.indexOf(title) === -1) titles.push(title);
    });
    if (!titles.length) return;

    api(['titles=' + titles.map(encodeURIComponent).join('%7C')])
      .then(function (json) {
        var q = json.query || {};
        /* MediaWiki reports title cleanups and redirects separately; follow
           both so a requested title lands on the page that answered. */
        var hops = {};
        (q.normalized || []).forEach(function (n) { hops[n.from] = n.to; });
        (q.redirects || []).forEach(function (r) { hops[r.from] = r.to; });

        var images = {};
        (q.pages || []).forEach(function (p) {
          images[p.title] = p.thumbnail ? p.thumbnail.source : null;
        });

        titles.forEach(function (title) {
          var final = title;
          for (var i = 0; i < 4 && hops[final]; i++) final = hops[final];
          var url = images[final];
          byArticle[title].forEach(function (id) {
            if (url) settle(id, url);
            else queueSearch(id);
          });
        });
        failures = 0;
        if (lookupQueue.length) scheduleBatch();
      })
      .catch(function () {
        /* A blip should not cost every remaining cover on the page: put the
           batch back and slow down. Only a persistently unreachable API —
           offline, or CORS-blocked — turns the whole thing off. */
        failures++;
        if (failures >= 3) {
          enabled = false;
          lookupQueue.length = 0;
          searchQueue.length = 0;
          return;
        }
        lookupQueue = ids.concat(lookupQueue);
        window.setTimeout(scheduleBatch, 1500 * failures);
      });
  }

  function queueSearch(id) {
    searchQueue.push(id);
    pumpSearches();
  }

  function pumpSearches() {
    while (enabled && searching < MAX_SEARCHES && searchQueue.length) {
      runSearch(searchQueue.shift());
    }
  }

  function runSearch(id) {
    var game = PG.data.byId[id];
    if (!game) return;
    searching++;
    var term = game.title + ' ' + game.year + ' video game';
    api(['generator=search', 'gsrlimit=1', 'gsrsearch=' + encodeURIComponent(term)])
      .then(function (json) {
        var page = ((json.query || {}).pages || [])[0];
        if (page && page.thumbnail) return page.thumbnail.source;
        /* Some articles are simply not indexed for page images — Undertale
           and Fortnite among them. Read the article's file list instead. */
        return hunt(page ? page.title : articleFor(id));
      })
      .then(function (url) { settle(id, url || 0); })
      .catch(function () { settle(id, 0); })
      .then(function () {
        searching--;
        pumpSearches();
      });
  }

  /* Wiki furniture that shows up in almost every article's file list. Kept
     specific — a bare /portal/ would throw away Portal's own cover. */
  var FURNITURE = /commons-logo|wikimedia|wikidata|wiki_?letter|wikiquote|wikinews|question_book|edit-clear|ambox|symbol_|padlock|semi-protection|disambig|folder_hexagonal|nuvola|crystal_|office-book|oojs|wpvg|red_pog|blue_pog|portal[-_ ]?icon|increase\.svg|decrease\.svg|steady\.svg/i;
  /* "poster" is deliberately absent: on Wikipedia it almost always means the
     film adaptation, and half this catalogue has one. */
  var COVERISH = /cover|box.?art|boxart|packshot/i;
  var LOGOISH = /logo|wordmark|title.?screen|key.?art/i;
  var ADAPTATION = /movie|film|tv[-_ ]?series|soundtrack|trailer|netflix/i;

  /* Words from the game's own name, used to check that a logo actually belongs
     to this game and is not the publisher's or the console's. */
  function tokensOf(title) {
    return String(title).replace(/[^A-Za-z0-9 ]/g, ' ').split(/\s+/)
      .filter(function (w) { return w.length >= 4; })
      .map(function (w) { return w.toLowerCase(); });
  }

  /* Words that are expected in a cover or logo filename and so do not count
     against it. Anything else — "marketplace", "first", "black", "beta" — marks
     a variant we would rather not use. */
  var EXPECTED = /^(logos?|wordmarks?|covers?|boxart|box|art|game|video|title|screen|key|official|vector|the|of|and|for|pc|eu|na|jp)$/;

  /* An article can list a dozen logos: historical ones, storefront ones,
     regional covers. Rank by how closely the filename matches just the game's
     name, then prefer the current version. */
  function rank(file, tokens) {
    var base = file.replace(/^File:/i, '').replace(/\.(png|svg|jpe?g)$/i, '');
    var words = base.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim().split(' ');
    var extras = 0;
    var year = 0;
    words.forEach(function (w) {
      if (/^(19|20)\d{2}$/.test(w)) { year = Math.max(year, parseInt(w, 10)); return; }
      if (EXPECTED.test(w)) return;
      if (tokens.some(function (t) { return w.indexOf(t) > -1 || t.indexOf(w) > -1; })) return;
      extras++;
    });
    return { extras: extras, year: year || 9999, len: base.length };
  }

  function best(files, tokens) {
    return files.slice().sort(function (a, b) {
      var ra = rank(a, tokens), rb = rank(b, tokens);
      return ra.extras - rb.extras || rb.year - ra.year || ra.len - rb.len;
    })[0];
  }

  function hunt(title) {
    if (!title) return null;
    return raw(['action=query', 'format=json', 'formatversion=2', 'origin=*',
      'redirects=1', 'prop=images', 'imlimit=40',
      'titles=' + encodeURIComponent(title)])
      .then(function (json) {
        var page = ((json.query || {}).pages || [])[0];
        var files = ((page && page.images) || [])
          .map(function (f) { return f.title; })
          .filter(function (f) {
            return /\.(jpe?g|png|svg)$/i.test(f) &&
              !FURNITURE.test(f) && !ADAPTATION.test(f);
          });

        var tokens = tokensOf(title);

        /* Only take a file that names itself as cover art. Falling back to
           "first image in the article" produces things like a photo of an
           E3 booth, which is worse than the generated cover. */
        var pick = best(files.filter(function (f) {
          return COVERISH.test(f) && !/\.svg$/i.test(f);
        }), tokens);

        /* A handful of the biggest games here — Minecraft, Roblox, Undertale —
           have no box art on Wikipedia at all, only a wordmark. Take the logo
           rather than leave them blank, but only when it carries the game's
           own name. */
        if (!pick) {
          pick = best(files.filter(function (f) {
            var lower = f.toLowerCase();
            return LOGOISH.test(lower) && tokens.some(function (t) {
              return lower.indexOf(t) > -1;
            });
          }), tokens);
        }
        if (!pick) return null;
        return raw(['action=query', 'format=json', 'formatversion=2', 'origin=*',
          'prop=imageinfo', 'iiprop=url', 'iiurlwidth=' + THUMB,
          'titles=' + encodeURIComponent(pick)])
          .then(function (json2) {
            var p = ((json2.query || {}).pages || [])[0];
            var info = p && p.imageinfo && p.imageinfo[0];
            return info ? (info.thumburl || info.url) : null;
          });
      });
  }

  /* ------------------------------------------------------------- presenting */

  function settle(id, url) {
    art[id] = url || 0;
    persist();
    var els = waiting[id] || [];
    delete waiting[id];
    if (!url) {
      els.forEach(function (el) { el.setAttribute('data-art', 'none'); });
      return;
    }
    els.forEach(function (el) { paint(el, url); });
  }

  /* Steam publishes a 600x900 portrait capsule for every app, which is close
     enough to the 3:4 frame to fill it edge to edge. When a row carries an app
     id there is nothing to look up at all — the URL is derivable — so these
     covers appear immediately and cost no API calls. */
  var STEAM_COVER = 'https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/';

  /* Games whose art is pinned by hand, because the resolver's best guess is
     wrong or worse than this. Minecraft and Fortnite have no cover art on
     Wikipedia at all — only wordmarks — and Minecraft's article sits next to a
     film of the same name, so leaving it to a search is asking for trouble. */
  var PINNED = {
    'minecraft': 'https://upload.wikimedia.org/wikipedia/en/b/be/Minecraft_game_logo_2023.png',
    'fortnite': 'https://upload.wikimedia.org/wikipedia/commons/thumb/0/0e/FortniteLogo.svg/960px-FortniteLogo.svg.png'
  };

  function steamCover(gameId) {
    if (PINNED[gameId]) return PINNED[gameId];
    var game = PG.data.byId[gameId];
    return game && game.steam ? STEAM_COVER + game.steam + '/library_600x900.jpg' : null;
  }

  function paint(el, url, onFail) {
    var pre = new window.Image();
    pre.onload = function () {
      if (el.querySelector('.cover__img')) return;
      var css = 'url("' + url.replace(/"/g, '%22') + '")';

      /* Roughly a third of what Wikipedia hands back is not box-art shaped:
         wide key art, banners and logos, some as extreme as 3:1.

         Filling the frame always looks better — bigger, sharper, no dead
         space — so the rule is a crop budget rather than a narrow band of
         allowed ratios: fill whenever the trim stays inside the budget for
         that kind of image, letterbox over a blurred copy of itself when it
         would not. */
      var ratio = pre.naturalHeight ? pre.naturalWidth / pre.naturalHeight : FRAME;
      var lost = ratio > FRAME ? 1 - FRAME / ratio : 1 - ratio / FRAME;
      var wordmark = /logo|wordmark/i.test(url);
      var boxish = lost <= (wordmark ? WORDMARK_CROP : MAX_CROP);

      /* A wordmark cannot be made big inside a portrait frame — Minecraft's is
         nearly 6:1 — so blurring a copy of it behind itself just wraps a thin
         strip in noise. Those go on the generated gradient instead: clean, high
         contrast, and already keyed to this game's colour. */
      if (!boxish && !wordmark) {
        var blur = document.createElement('div');
        blur.className = 'cover__blur';
        blur.style.backgroundImage = css;
        el.appendChild(blur);
      }

      var layer = document.createElement('div');
      layer.className = 'cover__img' +
        (boxish ? '' : (wordmark ? ' cover__img--mark' : ' cover__img--fit'));
      layer.style.backgroundImage = css;
      el.appendChild(layer);

      /* Flush the starting opacity so the fade has something to run from.
         A rAF would be tidier but never fires in a background tab, which
         would leave the art invisible until the tab was focused. */
      void layer.offsetWidth;
      el.setAttribute('data-art', 'ready');
      if (!boxish) el.classList.add(wordmark ? 'is-wordmark' : 'is-fitted');
      el.classList.add('has-art');
    };
    pre.onerror = function () {
      if (onFail) onFail();
      else el.setAttribute('data-art', 'none');
    };
    pre.src = url;
  }

  function want(el) {
    var id = el.getAttribute('data-game');
    if (!id) return;
    var known = art[id];
    if (known) { paint(el, known); return; }
    if (known === 0) { el.setAttribute('data-art', 'none'); return; }
    if (!enabled) return;
    (waiting[id] || (waiting[id] = [])).push(el);
    if (waiting[id].length > 1) return;   /* already requested */
    lookupQueue.push(id);
    scheduleBatch();
  }

  /* How far outside the viewport a cover is still worth fetching. Shared by the
     observer and the scroll fallback so both agree on what "near" means. */
  var NEAR = 400;

  var observer = window.IntersectionObserver ? new window.IntersectionObserver(
    function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        release(entry.target);
      });
    }, { rootMargin: NEAR + 'px 0px' }) : null;

  /* Covers held back for later, and the means to let them go.

     The observer is the primary trigger, but it cannot be the only one: it
     delivers nothing at all while a page is not producing frames, and some
     embedded surfaces never produce any — a headless preview pane will not fire
     even the initial callback the spec promises. Deferring on the observer alone
     would leave the lower half of a grid blank for good in those cases, so
     scroll position is also checked directly, and a short sweep after each
     render catches anything already on screen. */
  var pending = [];
  var sweepTimer = null;

  function defer(el) {
    pending.push(el);
    if (observer) observer.observe(el);
  }

  function release(el) {
    var i = pending.indexOf(el);
    if (i > -1) pending.splice(i, 1);
    if (observer) observer.unobserve(el);
    request(el);
  }

  function sweep() {
    sweepTimer = null;
    if (!pending.length) return;
    var h = window.innerHeight || document.documentElement.clientHeight;
    pending.slice().forEach(function (el) {
      /* A re-render can replace the element before it was ever needed. */
      if (el.isConnected === false || (el.isConnected === undefined &&
          !document.documentElement.contains(el))) {
        var i = pending.indexOf(el);
        if (i > -1) pending.splice(i, 1);
        if (observer) observer.unobserve(el);
        return;
      }
      var r = el.getBoundingClientRect();
      if (r.bottom > -NEAR && r.top < h + NEAR) release(el);
    });
  }

  function scheduleSweep() {
    if (sweepTimer) return;
    sweepTimer = window.setTimeout(sweep, 120);
  }

  window.addEventListener('scroll', scheduleSweep, { passive: true });
  window.addEventListener('resize', scheduleSweep);

  /* Three ways to reach a URL, all of which end in an image fetch:
       - the cache holds one from an earlier session,
       - a Steam app id derives one with no lookup at all,
       - otherwise the Wikipedia pipeline has to go and find one.
     The cache saves the *lookup*, not the download, which is why a cache hit is
     budgeted the same as everything else. */
  function request(el) {
    var id = el.getAttribute('data-game');
    if (!id) return;
    if (art[id]) { paint(el, art[id]); return; }
    var steam = steamCover(id);
    if (steam) {
      /* If the capsule turns out to be missing, that one game drops through to
         the Wikipedia pipeline. */
      paint(el, steam, function () { want(el); });
      return;
    }
    want(el);
  }

  /* Called after every render. A screenful goes out at once and the rest is
     deferred, so a 48-tile grid does not fetch every cover below the fold
     before the reader has scrolled. */
  function hydrate(root) {
    var scope = root || document;
    var els = scope.querySelectorAll('.cover[data-game]:not([data-art])');
    var eager = 0;
    Array.prototype.forEach.call(els, function (el) {
      var id = el.getAttribute('data-game');
      el.setAttribute('data-art', 'wait');

      /* Known to have no art: nothing to fetch, so nothing to defer. */
      if (art[id] === 0) { el.setAttribute('data-art', 'none'); return; }

      /* An unconditional first batch, so a page that never scrolls and never
         paints a frame still shows covers. */
      if (eager < EAGER_LIMIT) {
        eager++;
        request(el);
        return;
      }
      defer(el);
    });
    /* The budget is a floor, not a cap: a tall window may already be showing
       more than EAGER_LIMIT tiles, and those should not wait for a scroll. */
    scheduleSweep();
  }

  PG.art = {
    hydrate: hydrate,
    /* Handy from the console: PG.art.forget() then reload to re-fetch. */
    forget: function () {
      art = {};
      try { window.localStorage.removeItem(CACHE_KEY); } catch (e) {}
    },
    disable: function () { enabled = false; },
    stats: function () {
      var found = 0, missing = 0;
      Object.keys(art).forEach(function (k) { art[k] ? found++ : missing++; });
      var steam = PG.data.games.filter(function (g) { return g.steam; }).length;
      return {
        found: found, missing: missing, enabled: enabled,
        steamCovers: steam, needLookup: PG.data.games.length - steam
      };
    }
  };
})(window.PG);
