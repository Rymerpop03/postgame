/* Postgame — data layer
   Parses the raw catalogue rows, builds lookup indexes, and holds the seeded
   community (demo members + their diary entries). No network calls: the whole
   site runs from these files so it works straight off the filesystem. */
window.PG = window.PG || {};
(function (PG) {
  'use strict';

  /* ---------------------------------------------------------------- helpers */

  function slugify(str) {
    return str
      .toLowerCase()
      .replace(/&/g, ' and ')
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '');
  }

  /* Deterministic hash + PRNG so every generated value (cover hue, community
     rating spread, review like counts) is stable between page loads. */
  function hash32(str) {
    var h = 2166136261;
    for (var i = 0; i < str.length; i++) {
      h ^= str.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return h >>> 0;
  }

  function rng(seed) {
    var a = seed >>> 0;
    return function () {
      a = (a + 0x6d2b79f5) >>> 0;
      var t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  /* ---------------------------------------------------------------- parsing */

  var COVER_PATTERNS = ['grid', 'rays', 'orbs', 'stripes', 'arcs', 'noise'];

  var rows = window.GAME_ROWS || [];
  /* Scaled to the catalogue rather than a fixed number: a hard 520 sent reach
     negative once the list passed that length, and a negative base with a
     fractional exponent is NaN — which silently wiped out every rating. */
  var span = rows.length + 40;

  var games = rows.map(function (row, index) {
    var p = row.split('|');
    var title = p[0];
    var seed = hash32(title);
    var r = rng(seed);
    /* Rank 0 is the biggest game in the catalogue; the list is authored in
       rough order of cultural footprint, so index doubles as popularity. */
    var reach = Math.max(0.02, 1 - index / span);
    return {
      id: slugify(title),
      title: title,
      year: parseInt(p[1], 10),
      developer: p[2],
      genres: p[3].split(','),
      platforms: p[4].split(','),
      critic: parseInt(p[5], 10) || null,
      blurb: p[6] || '',
      /* Steam-sourced rows carry two extra fields: the app id, which gives us
         a uniform 600x900 cover straight from Steam's CDN, and the percentage
         of Steam reviews that are positive. Older rows have neither. */
      steam: p[7] ? parseInt(p[7], 10) || null : null,
      userScore: p[8] ? parseInt(p[8], 10) || null : null,
      rank: index,
      reach: reach,
      hue: Math.floor(r() * 360),
      pattern: COVER_PATTERNS[seed % COVER_PATTERNS.length],
      seed: seed
    };
  });

  var byId = Object.create(null);
  var byTitle = Object.create(null);
  games.forEach(function (g) {
    byId[g.id] = g;
    byTitle[g.title.toLowerCase()] = g;
  });

  /* ------------------------------------------------- community rating curve */

  /* Each game carries a stable distribution of member ratings so every one of
     the 500 pages has a believable score and histogram. It is centred on the
     critic score, widened a little, and jittered per bucket. */
  var BUCKETS = [0.5, 1, 1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5];

  /* A Steam "% positive" is not a quality score out of 100 — almost everything
     sits between 70 and 97 — so it gets its own curve rather than being divided
     by 20: 50% lands near 2.5 stars, 85% near 4, 97% near 4.6. */
  function fromUserScore(pct) {
    return Math.max(1.2, Math.min(4.7, 2.5 + (pct - 50) / 48 * 2.1));
  }

  games.forEach(function (g) {
    var r = rng(g.seed ^ 0x9e3779b9);
    var centre = g.critic ? g.critic / 20 :
      (g.userScore ? fromUserScore(g.userScore) : 3.4);
    centre = Math.max(1.6, Math.min(4.75, centre - 0.15 + r() * 0.3));
    var spread = 0.52 + r() * 0.42;
    var total = Math.round(18 + Math.pow(g.reach, 2.6) * 5200 * (0.55 + r() * 0.9));

    var weights = BUCKETS.map(function (v) {
      var d = v - centre;
      return Math.exp(-(d * d) / (2 * spread * spread)) * (0.82 + r() * 0.36);
    });
    var sum = weights.reduce(function (a, b) { return a + b; }, 0);

    var counts = weights.map(function (w) {
      return Math.max(0, Math.round((w / sum) * total));
    });
    g.baseCounts = counts;
  });

  /* ------------------------------------------------------------- taxonomies */

  function tally(key) {
    var map = Object.create(null);
    games.forEach(function (g) {
      g[key].forEach(function (v) { map[v] = (map[v] || 0) + 1; });
    });
    return Object.keys(map)
      .sort(function (a, b) { return map[b] - map[a] || a.localeCompare(b); })
      .map(function (name) { return { name: name, count: map[name] }; });
  }

  var genres = tally('genres');
  var platforms = tally('platforms');

  var decades = [];
  (function () {
    var seen = Object.create(null);
    games.forEach(function (g) {
      var d = Math.floor(g.year / 10) * 10;
      seen[d] = (seen[d] || 0) + 1;
    });
    decades = Object.keys(seen)
      .map(Number)
      .sort(function (a, b) { return b - a; })
      .map(function (d) { return { decade: d, count: seen[d] }; });
  })();

  /* ----------------------------------------------------------------- search */

  function search(query, limit) {
    var q = query.trim().toLowerCase();
    if (!q) return [];
    var out = [];
    for (var i = 0; i < games.length; i++) {
      var g = games[i];
      var t = g.title.toLowerCase();
      var score = -1;
      if (t === q) score = 0;
      else if (t.indexOf(q) === 0) score = 1;
      else if (t.indexOf(' ' + q) > -1) score = 2;
      else if (t.indexOf(q) > -1) score = 3;
      else if (g.developer.toLowerCase().indexOf(q) > -1) score = 5;
      if (score > -1) out.push({ game: g, score: score });
    }
    out.sort(function (a, b) {
      return a.score - b.score || a.game.rank - b.game.rank;
    });
    return out.slice(0, limit || 40).map(function (o) { return o.game; });
  }

  /* ------------------------------------------------------- seeded community */

  var MEMBERS = [
    ['pixelvagrant', 'Ash Moreau', 268, 'Backlog abolitionist. If it has a parry window, I have died to it.'],
    ['quinnbyte', 'Quinn Ledger', 32, 'I finish games. Eventually. Ask me about my 40-hour save files.'],
    ['neonharbor', 'Rae Okonkwo', 196, 'Immersive sims, level design, and very long walks through office buildings.'],
    ['sixteenbit', 'Tomas Beier', 44, 'Cartridge collector, 1985 to 1999. Everything after that is a remake.'],
    ['haloweenie', 'Devin Park', 12, 'Shooters, co-op, and one Destiny addiction I am not discussing.'],
    ['moonlitmage', 'Ingrid Salas', 288, 'JRPGs, tea, and 200-hour saves I refuse to abandon.'],
    ['framerate_fran', 'Fran Ito', 340, 'Frame data enjoyer. Still bronze at aerials.'],
    ['couchcoop_kev', 'Kev Duarte', 88, 'Everything is better with a second controller.'],
    ['hollowbee', 'Bee Nakamura', 158, 'Metroidvanias, sad platformers, and notebooks full of maps.'],
    ['glitchwitch', 'Nour Haddad', 220, 'Roguelikes, splits, and questionable sleep hygiene.']
  ].map(function (m) {
    return { username: m[0], name: m[1], hue: m[2], bio: m[3], seeded: true };
  });

  /* [member, game title, rating, liked, date, review] — review may be empty
     for a rating-only diary entry. */
  var ENTRIES = [
    ['pixelvagrant', 'Elden Ring', 5, 1, '2022-03-20', 'Spent four hours in the first field and never once touched the main road. No other game trusts you like this.'],
    ['pixelvagrant', 'Dark Souls', 4.5, 1, '2021-11-02', 'Lordran folds back on itself like origami. When the elevator drops into Firelink I said something out loud.'],
    ['pixelvagrant', 'Sekiro: Shadows Die Twice', 5, 1, '2023-01-08', 'Once the deflect rhythm clicks it stops being a game about health bars and becomes a conversation.'],
    ['pixelvagrant', 'Bloodborne', 5, 1, '2022-06-11', ''],
    ['pixelvagrant', 'Hollow Knight: Silksong', 4.5, 1, '2025-09-12', 'Harder than it needs to be and I would not change a thing. Hornet moves like a threat.'],
    ['pixelvagrant', 'Lies of P', 3.5, 0, '2023-10-01', 'A very good student. The parry window is tighter than anything FromSoftware has ever asked of me.'],
    ['pixelvagrant', 'Elden Ring Nightreign', 3, 0, '2025-06-02', 'Great with three friends, thin on your own.'],
    ['pixelvagrant', 'Nioh 2', 4, 0, '2020-04-19', ''],
    ['pixelvagrant', "Demon's Souls", 4.5, 1, '2021-01-05', 'The remake is almost too clean, but the Tower of Latria still feels like a nightmare someone recorded.'],
    ['pixelvagrant', 'Armored Core VI: Fires of Rubicon', 4, 0, '2023-09-02', 'Balteus took me eleven hours across three nights. Worth all of them.'],
    ['pixelvagrant', 'Doom (2016)', 4, 0, '2019-07-07', ''],
    ['pixelvagrant', 'Ghost of Yotei', 4.5, 1, '2025-10-20', 'Every duel is framed like a painting and the wind still does the navigating.'],

    ['quinnbyte', "Baldur's Gate 3", 5, 1, '2023-08-30', 'I talked a bear out of a fight and then made steadily worse decisions for ninety hours. Perfect game.'],
    ['quinnbyte', 'Grand Theft Auto V', 4, 0, '2014-02-02', ''],
    ['quinnbyte', 'Red Dead Redemption 2', 4.5, 1, '2019-01-12', 'The slowest game I have ever loved. Half my playtime is riding somewhere in the dark, on purpose.'],
    ['quinnbyte', 'Stardew Valley', 4.5, 1, '2018-05-06', 'Bought it to relax. I now maintain a spreadsheet.'],
    ['quinnbyte', 'Balatro', 5, 1, '2024-03-01', 'I lost an entire weekend to this and I have no further notes.'],
    ['quinnbyte', 'Cyberpunk 2077', 3.5, 0, '2021-02-14', 'Night City is astonishing and the game around it took two more years to catch up to it.'],
    ['quinnbyte', 'Fortnite', 3, 0, '2020-09-09', ''],
    ['quinnbyte', 'It Takes Two', 4, 1, '2021-06-20', 'Played the whole thing with my partner. We are still together. Barely.'],
    ['quinnbyte', 'Untitled Goose Game', 4, 1, '2019-10-02', 'Honk.'],
    ['quinnbyte', 'Split Fiction', 4.5, 1, '2025-03-16', 'Hazelight keep inventing mechanics, using them once, and throwing them away. Absurd generosity.'],
    ['quinnbyte', 'Helldivers 2', 4, 0, '2024-02-25', 'Democracy, but the fun kind of chaos. Never trust a friend with a 500kg bomb.'],
    ['quinnbyte', 'The Witcher 3: Wild Hunt', 5, 1, '2016-01-04', 'The Bloody Baron questline. That is the entire review.'],

    ['neonharbor', 'Dishonored 2', 5, 1, '2016-11-20', 'The Clockwork Mansion is the best level anyone has built. I have now played it six different ways.'],
    ['neonharbor', 'Prey', 4.5, 1, '2017-06-01', 'Trust nothing. The GLOO cannon is a lockpick, a ladder and a weapon depending on how tired you are.'],
    ['neonharbor', 'Deus Ex', 4.5, 1, '2012-08-11', 'Still asks better questions than games with twenty times the budget.'],
    ['neonharbor', 'Thief II: The Metal Age', 4, 0, '2013-03-03', ''],
    ['neonharbor', 'BioShock', 4.5, 1, '2010-05-15', 'Rapture, and the first time a game made me feel genuinely complicit in something.'],
    ['neonharbor', 'Hitman 3', 4.5, 1, '2021-02-02', 'Sapienza and Berlin are architecture, not levels. I have never used a single guide.'],
    ['neonharbor', 'System Shock 2', 4, 0, '2011-09-09', ''],
    ['neonharbor', 'Outer Wilds', 5, 1, '2020-01-19', 'Knowledge is the only upgrade. I have never been so careful about how I recommend a game.'],
    ['neonharbor', 'Return of the Obra Dinn', 4.5, 1, '2019-02-14', 'A spreadsheet that slowly becomes a ghost story.'],
    ['neonharbor', 'Deathloop', 4, 0, '2021-10-01', ''],
    ['neonharbor', 'Control', 4, 1, '2019-09-30', 'The Oldest House deserves an architecture prize. The combat is a bonus.'],
    ['neonharbor', 'Half-Life 2', 5, 1, '2009-04-01', 'Ravenholm. Still.'],

    ['sixteenbit', 'Super Metroid', 5, 1, '2015-02-20', 'Wordless design that never once loses you. Thirty years old and still teaching studios how to do it.'],
    ['sixteenbit', 'Chrono Trigger', 5, 1, '2014-08-08', ''],
    ['sixteenbit', 'The Legend of Zelda: A Link to the Past', 5, 1, '2016-06-06', 'A dungeon that changes the world outside it. Nothing has topped that trick.'],
    ['sixteenbit', 'Super Mario World', 4.5, 1, '2013-05-05', ''],
    ['sixteenbit', 'Tetris', 4.5, 0, '2012-01-01', 'Seven shapes. Whole life.'],
    ['sixteenbit', 'Castlevania: Symphony of the Night', 5, 1, '2017-10-31', 'The inverted castle is either the greatest twist in the genre or the laziest. Both, honestly.'],
    ['sixteenbit', 'Mega Man 2', 4.5, 0, '2015-11-11', ''],
    ['sixteenbit', 'Final Fantasy VI', 5, 1, '2016-09-19', 'They put an opera on a cartridge in 1994 and it still works.'],
    ['sixteenbit', 'Pac-Man', 4, 0, '2011-03-03', ''],
    ['sixteenbit', 'Tekken 3', 4.5, 1, '2018-04-12', 'Arcade perfect, and it still feels correct twenty years later.'],
    ['sixteenbit', 'GoldenEye 007', 4, 1, '2014-12-24', 'Four friends, one screen, Oddjob permanently banned.'],
    ['sixteenbit', 'Donkey Kong Country', 4, 0, '2013-09-09', ''],

    ['haloweenie', 'Halo 3', 5, 1, '2010-08-08', 'Finish the fight. I can still hear the Warthog run without putting the disc in.'],
    ['haloweenie', 'Halo: Combat Evolved', 4.5, 1, '2009-11-15', ''],
    ['haloweenie', 'Titanfall 2', 5, 1, '2017-01-22', 'The best FPS campaign of its decade and nobody bought it. Effect and Cause. Go and play it.'],
    ['haloweenie', 'Destiny 2', 4, 0, '2018-09-09', 'The shooting is untouchable. Everything around it is a second job I keep choosing.'],
    ['haloweenie', 'Call of Duty 4: Modern Warfare', 4.5, 1, '2010-02-02', ''],
    ['haloweenie', 'Apex Legends', 4, 1, '2019-04-04', 'Movement plus the ping system. Every shooter since should have stolen both.'],
    ['haloweenie', 'Doom Eternal', 4.5, 1, '2020-04-01', 'Resource management disguised as heavy metal. The dash changes everything.'],
    ['haloweenie', 'Left 4 Dead 2', 4.5, 1, '2011-06-06', ''],
    ['haloweenie', 'Battlefield 1', 4, 0, '2017-03-03', 'The 64-player operations are the loudest thing in games.'],
    ['haloweenie', 'Halo Infinite', 3.5, 0, '2022-01-05', 'The grapple is perfect. The rest of it arrived in pieces over two years.'],
    ['haloweenie', 'Deep Rock Galactic', 4.5, 1, '2021-07-04', 'Rock and stone. Genuinely the friendliest lobby in multiplayer.'],
    ['haloweenie', 'Battlefield 6', 4, 0, '2025-11-02', 'It remembered what it was supposed to be.'],

    ['moonlitmage', 'Persona 5 Royal', 5, 1, '2020-04-04', 'A hundred and twenty hours and I would start again tomorrow. The confidants are the actual game.'],
    ['moonlitmage', 'Final Fantasy VII', 4.5, 1, '2015-01-15', ''],
    ['moonlitmage', 'Clair Obscur: Expedition 33', 5, 1, '2025-05-04', 'Parries have no business working this well in a turn-based RPG. The soundtrack is doing something illegal.'],
    ['moonlitmage', 'Metaphor: ReFantazio', 4.5, 1, '2024-11-01', 'Atlus turned an election into a dungeon crawl and somehow it is their most focused game.'],
    ['moonlitmage', 'Xenoblade Chronicles 3', 4.5, 0, '2022-09-01', ''],
    ['moonlitmage', 'Dragon Quest XI', 4.5, 1, '2018-10-10', 'Comfort food cooked by professionals. Not one surprise, all of it excellent.'],
    ['moonlitmage', 'Nier: Automata', 5, 1, '2018-03-22', 'Ending E. That is all I am going to say about it.'],
    ['moonlitmage', 'Fire Emblem: Three Houses', 4, 0, '2019-08-08', ''],
    ['moonlitmage', 'Persona 3 Reload', 4, 1, '2024-03-03', 'Tartarus is still Tartarus, but everything built around it is gorgeous now.'],
    ['moonlitmage', 'Final Fantasy VII Rebirth', 4.5, 1, '2024-04-01', 'The world opened up and I got lost in every single minigame. No regrets, no notes.'],
    ['moonlitmage', 'Deltarune', 4.5, 1, '2025-07-01', 'Toby Fox has no business making music this good on his own.'],
    ['moonlitmage', 'Chrono Trigger', 5, 1, '2017-07-07', ''],

    ['framerate_fran', 'Street Fighter 6', 4.5, 1, '2023-06-10', 'Drive Rush changed the whole game, and World Tour is the best teaching tool a fighter has shipped with.'],
    ['framerate_fran', 'Guilty Gear Strive', 4.5, 1, '2021-06-25', 'The prettiest fighting game ever made and the netcode finally deserves it.'],
    ['framerate_fran', 'Tekken 8', 4, 0, '2024-02-01', 'Heat is aggressive to a fault. I have not stopped playing.'],
    ['framerate_fran', 'Super Smash Bros. Melee', 4.5, 1, '2016-03-03', ''],
    ['framerate_fran', 'Dragon Ball FighterZ', 4, 1, '2018-02-02', 'Looks exactly like the show, plays like a real fighting game. Rare combination.'],
    ['framerate_fran', 'Counter-Strike 2', 3.5, 0, '2023-10-10', 'Same game, new lighting, my aim is still the problem.'],
    ['framerate_fran', 'Rocket League', 4.5, 1, '2017-08-08', 'Eight years in and I am somehow still bronze at aerials.'],
    ['framerate_fran', 'Valorant', 4, 0, '2020-07-07', ''],
    ['framerate_fran', 'Mortal Kombat 11', 3.5, 0, '2019-05-05', 'Great story mode. The cosmetic grind is genuinely hostile.'],
    ['framerate_fran', 'Dota 2', 4, 0, '2015-09-09', 'The best and the worst thing I have ever installed.'],
    ['framerate_fran', 'Marvel vs. Capcom 2', 4.5, 1, '2014-04-04', ''],

    ['couchcoop_kev', 'Mario Kart 8 Deluxe', 5, 1, '2018-01-01', 'Nine years of birthday parties and it has never once been the wrong choice.'],
    ['couchcoop_kev', 'Overcooked 2', 4, 0, '2019-02-02', 'Genuinely stress-tested my marriage. Recommended.'],
    ['couchcoop_kev', 'Super Smash Bros. Ultimate', 4.5, 1, '2019-01-15', ''],
    ['couchcoop_kev', 'Wii Sports', 4, 1, '2010-04-04', 'Bowling with my grandmother. Still the most important game in this house.'],
    ['couchcoop_kev', 'Among Us', 4, 0, '2020-10-10', ''],
    ['couchcoop_kev', 'Fall Guys', 3.5, 0, '2020-08-15', 'Perfect in twenty minute doses, exhausting in sixty.'],
    ['couchcoop_kev', 'It Takes Two', 4.5, 1, '2022-02-14', 'The best co-op design since Portal 2 and I will not be taking questions.'],
    ['couchcoop_kev', 'Portal 2', 5, 1, '2012-05-05', 'The co-op campaign is the reason I still own a second controller.'],
    ['couchcoop_kev', 'Mario Party Superstars', 4, 0, '2021-12-25', 'The boards are great. The friendships are not.'],
    ['couchcoop_kev', 'Split Fiction', 4.5, 1, '2025-04-01', ''],
    ['couchcoop_kev', 'Human: Fall Flat', 3.5, 0, '2018-06-06', ''],
    ['couchcoop_kev', 'Minecraft', 4.5, 1, '2015-12-12', 'Built a house with my kid. Ten years later we still load that world.'],

    ['hollowbee', 'Hollow Knight', 5, 1, '2018-02-02', 'Hallownest is the most confident world in games. Greenpath at dusk lives permanently in my head.'],
    ['hollowbee', 'Hollow Knight: Silksong', 5, 1, '2025-09-06', 'Seven years, and worth every one. Hornet is a completely different animal to the Knight.'],
    ['hollowbee', 'Celeste', 5, 1, '2018-03-03', 'The assist menu is a design masterclass. Chapter 9 nearly finished me off.'],
    ['hollowbee', 'Ori and the Will of the Wisps', 4.5, 1, '2020-04-04', ''],
    ['hollowbee', 'Blasphemous', 3.5, 0, '2020-01-01', 'Gorgeous, and a little too pleased with how cruel its checkpoints are.'],
    ['hollowbee', 'Nine Sols', 4.5, 1, '2024-06-06', 'The deflect is Sekiro on a 2D plane and it absolutely sings.'],
    ['hollowbee', 'Cave Story', 4.5, 1, '2017-04-04', ''],
    ['hollowbee', 'Rain World', 4, 0, '2019-05-05', 'An ecosystem that genuinely does not care whether you live. Respect.'],
    ['hollowbee', 'Dead Cells', 4.5, 1, '2019-06-06', ''],
    ['hollowbee', 'Tunic', 4.5, 1, '2022-04-04', 'The manual is the game. I filled an actual paper notebook.'],
    ['hollowbee', 'Metroid Dread', 4, 0, '2021-11-11', ''],
    ['hollowbee', 'Inside', 4.5, 1, '2017-01-01', 'The last twelve seconds justify the entire thing.'],

    ['glitchwitch', 'Hades', 5, 1, '2020-10-10', 'Two hundred escape attempts and the writing never once repeated on me. Still unmatched.'],
    ['glitchwitch', 'Hades II', 4.5, 1, '2025-10-01', 'More of everything. Melinoe is a harder, meaner protagonist and the game is meaner with her.'],
    ['glitchwitch', 'Slay the Spire', 5, 1, '2019-03-03', ''],
    ['glitchwitch', 'Vampire Survivors', 4.5, 1, '2022-11-11', 'Cost less than a coffee. Ninety hours later I am still embarrassed.'],
    ['glitchwitch', 'Risk of Rain 2', 4.5, 1, '2020-09-09', ''],
    ['glitchwitch', 'Into the Breach', 5, 1, '2018-04-04', 'Perfect information, zero mercy. Chess, if chess had mechs and an eight turn timer.'],
    ['glitchwitch', 'Spelunky 2', 4.5, 1, '2021-02-02', 'The systems conspire against you and it is always, provably, your own fault.'],
    ['glitchwitch', 'Returnal', 4, 0, '2021-05-05', ''],
    ['glitchwitch', 'Balatro', 5, 1, '2024-02-22', 'I have started dreaming about jokers. This is a genuine problem.'],
    ['glitchwitch', 'Crypt of the NecroDancer', 4, 0, '2016-06-06', ''],
    ['glitchwitch', 'Enter the Gungeon', 4, 0, '2017-07-07', ''],
    ['glitchwitch', 'Dead Cells', 4.5, 1, '2020-03-03', ''],

    /* ---- the games everybody has played ----------------------------------
       Up to here each member logs only their own niche, which is realistic per
       person but leaves 40 of the 45 member pairs with *zero* games in common —
       nothing for a taste comparison to compare. These are the crossover hits
       almost everyone has an opinion on. The ratings are not filler: they are
       written to disagree along the lines the members already disagree on, so
       the matches that come out are about taste rather than coincidence. */

    ['pixelvagrant', 'Portal 2', 4, 0, '2013-02-02', ''],
    ['quinnbyte', 'Portal 2', 4.5, 1, '2012-07-07', ''],
    ['neonharbor', 'Portal 2', 5, 1, '2011-05-05', 'Every chamber teaches you something and then asks for it back with interest.'],
    ['sixteenbit', 'Portal 2', 4, 0, '2014-03-03', ''],
    ['haloweenie', 'Portal 2', 4.5, 1, '2011-06-06', ''],
    ['moonlitmage', 'Portal 2', 4, 0, '2013-08-08', ''],
    ['framerate_fran', 'Portal 2', 3.5, 0, '2015-01-01', ''],
    ['hollowbee', 'Portal 2', 4.5, 1, '2012-02-02', ''],
    ['glitchwitch', 'Portal 2', 4, 0, '2016-04-04', ''],

    ['pixelvagrant', 'The Legend of Zelda: Breath of the Wild', 4.5, 1, '2017-04-04', 'The only open world that rewards curiosity instead of clearing icons.'],
    ['quinnbyte', 'The Legend of Zelda: Breath of the Wild', 5, 1, '2017-03-20', 'I put 190 hours into this and finished maybe a third of it. No regrets.'],
    ['neonharbor', 'The Legend of Zelda: Breath of the Wild', 4.5, 1, '2017-06-06', ''],
    ['sixteenbit', 'The Legend of Zelda: Breath of the Wild', 3.5, 0, '2018-01-01', 'Enormous and beautiful, and I still reach for A Link to the Past.'],
    ['haloweenie', 'The Legend of Zelda: Breath of the Wild', 4, 0, '2017-09-09', ''],
    ['moonlitmage', 'The Legend of Zelda: Breath of the Wild', 4.5, 1, '2017-05-05', ''],
    ['framerate_fran', 'The Legend of Zelda: Breath of the Wild', 3.5, 0, '2018-02-02', ''],
    ['couchcoop_kev', 'The Legend of Zelda: Breath of the Wild', 4, 0, '2017-08-08', ''],
    ['hollowbee', 'The Legend of Zelda: Breath of the Wild', 4, 0, '2017-07-07', ''],
    ['glitchwitch', 'The Legend of Zelda: Breath of the Wild', 4, 0, '2018-03-03', ''],

    ['pixelvagrant', 'Hades', 4.5, 1, '2021-01-20', 'Death as a narrative device, done properly for once.'],
    ['quinnbyte', 'Hades', 4, 0, '2021-03-03', ''],
    ['neonharbor', 'Hades', 4, 0, '2021-04-04', ''],
    ['sixteenbit', 'Hades', 3.5, 0, '2021-06-06', ''],
    ['haloweenie', 'Hades', 4, 0, '2021-02-02', ''],
    ['moonlitmage', 'Hades', 4.5, 1, '2020-12-12', 'The only roguelike whose cast I actually care about.'],
    ['framerate_fran', 'Hades', 4, 0, '2021-05-05', ''],
    ['couchcoop_kev', 'Hades', 3.5, 0, '2021-07-07', ''],
    ['hollowbee', 'Hades', 4.5, 1, '2020-11-11', ''],

    ['pixelvagrant', 'Celeste', 4.5, 1, '2018-06-06', 'Every death is legible. That is the whole trick and almost nobody manages it.'],
    ['quinnbyte', 'Celeste', 4, 0, '2018-09-09', ''],
    ['neonharbor', 'Celeste', 4, 0, '2019-01-01', ''],
    ['sixteenbit', 'Celeste', 4, 0, '2018-11-11', ''],
    ['haloweenie', 'Celeste', 3.5, 0, '2019-03-03', ''],
    ['moonlitmage', 'Celeste', 4, 0, '2018-08-08', ''],
    ['framerate_fran', 'Celeste', 4.5, 1, '2018-05-05', 'Frame-perfect and it never once feels unfair. The dash has real weight.'],
    ['couchcoop_kev', 'Celeste', 3.5, 0, '2019-05-05', ''],
    ['glitchwitch', 'Celeste', 5, 1, '2018-04-04', 'I have a golden strawberry and a repetitive strain injury.'],

    ['pixelvagrant', 'Minecraft', 3, 0, '2016-01-01', ''],
    ['quinnbyte', 'Minecraft', 4.5, 1, '2013-03-03', 'Fifteen years on and I still start a new world every winter.'],
    ['neonharbor', 'Minecraft', 3.5, 0, '2015-05-05', ''],
    ['sixteenbit', 'Minecraft', 3.5, 0, '2014-04-04', ''],
    ['haloweenie', 'Minecraft', 3.5, 0, '2015-09-09', ''],
    ['moonlitmage', 'Minecraft', 4, 0, '2014-10-10', ''],
    ['hollowbee', 'Minecraft', 3.5, 0, '2016-06-06', ''],
    ['glitchwitch', 'Minecraft', 3.5, 0, '2017-02-02', ''],

    ['quinnbyte', 'Tetris', 4, 0, '2015-05-05', ''],
    ['haloweenie', 'Tetris', 4, 0, '2014-07-07', ''],
    ['moonlitmage', 'Tetris', 3.5, 0, '2016-02-02', ''],
    ['framerate_fran', 'Tetris', 4.5, 1, '2013-09-09', 'The original execution game. Stacking is neutral, topping out is losing neutral.'],
    ['couchcoop_kev', 'Tetris', 4, 0, '2015-11-11', ''],
    ['glitchwitch', 'Tetris', 4.5, 1, '2017-08-08', ''],

    ['pixelvagrant', 'Undertale', 3.5, 0, '2017-03-03', ''],
    ['quinnbyte', 'Undertale', 4, 0, '2016-08-08', ''],
    ['neonharbor', 'Undertale', 4, 0, '2016-05-05', ''],
    ['moonlitmage', 'Undertale', 4.5, 1, '2016-01-15', 'A game about refusing to play it the way every other game taught you.'],
    ['hollowbee', 'Undertale', 4.5, 1, '2016-03-03', ''],
    ['glitchwitch', 'Undertale', 4, 0, '2017-05-05', ''],

    ['pixelvagrant', 'Half-Life 2', 4, 0, '2012-01-01', ''],
    ['quinnbyte', 'Half-Life 2', 4.5, 1, '2011-11-11', ''],
    ['sixteenbit', 'Half-Life 2', 4.5, 1, '2010-10-10', ''],
    ['haloweenie', 'Half-Life 2', 5, 1, '2010-03-03', 'The gravity gun is still the best toy anyone has put in a shooter.'],
    ['framerate_fran', 'Half-Life 2', 3.5, 0, '2014-06-06', ''],

    ['quinnbyte', 'Mario Kart 8 Deluxe', 4.5, 1, '2018-04-04', ''],
    ['sixteenbit', 'Mario Kart 8 Deluxe', 4.5, 1, '2018-07-07', ''],
    ['haloweenie', 'Mario Kart 8 Deluxe', 4, 0, '2019-02-02', ''],
    ['moonlitmage', 'Mario Kart 8 Deluxe', 4, 0, '2019-06-06', ''],
    ['framerate_fran', 'Mario Kart 8 Deluxe', 4, 0, '2018-10-10', ''],
    ['glitchwitch', 'Mario Kart 8 Deluxe', 3.5, 0, '2019-09-09', ''],

    ['neonharbor', 'Dark Souls', 4, 0, '2014-02-02', ''],
    ['hollowbee', 'Dark Souls', 4, 0, '2015-08-08', ''],
    ['glitchwitch', 'Dark Souls', 4.5, 1, '2016-09-09', '']
  ];

  /* Play state for the demo diary: a few members are mid-playthrough, a few
     gave up, everything else reads as finished. */
  var SEED_STATE = {
    'pixelvagrant::ghost-of-yotei': 'playing',
    'pixelvagrant::elden-ring-nightreign': 'abandoned',
    'quinnbyte::helldivers-2': 'playing',
    'quinnbyte::cyberpunk-2077': 'abandoned',
    'quinnbyte::fortnite': 'abandoned',
    'neonharbor::deathloop': 'playing',
    'haloweenie::destiny-2': 'playing',
    'haloweenie::halo-infinite': 'abandoned',
    'moonlitmage::metaphor-refantazio': 'playing',
    'moonlitmage::final-fantasy-vii-rebirth': 'playing',
    'framerate_fran::tekken-8': 'playing',
    'framerate_fran::counter-strike-2': 'abandoned',
    'framerate_fran::mortal-kombat-11': 'abandoned',
    'couchcoop_kev::mario-kart-8-deluxe': 'playing',
    'couchcoop_kev::fall-guys': 'abandoned',
    'hollowbee::hollow-knight-silksong': 'playing',
    'hollowbee::blasphemous': 'abandoned',
    'glitchwitch::hades-ii': 'playing',
    'glitchwitch::returnal': 'abandoned'
  };

  /* Plausible hours by genre, so the totals and the "hours logged" stat are
     not all zero on a first visit. This is seed data for fake accounts — the
     same fiction as their ratings — not a claim about anyone's real playtime. */
  var HOUR_RANGES = [
    [/MMO|Colony Sim|Sandbox|Life Sim|Farming/, 70, 240],
    [/Open World|Action RPG|JRPG|RPG|Simulation|Survival|Management|City Builder|Strategy|Gacha/, 38, 130],
    [/Roguelike|MOBA|Battle Royale|Card Game|Fighting|Sports|Racing|Auto Battler|Party/, 20, 95],
    [/Souls-like|Metroidvania|Tactics|Action Adventure|Stealth|Immersive Sim|Hack and Slash/, 18, 62],
    [/Platformer|Puzzle|Horror|FPS|TPS|Shooter|Visual Novel|Point and Click|Adventure|Rhythm/, 7, 28]
  ];

  function seedHours(game, r) {
    var lo = 10, hi = 35;
    var genres = game.genres.join(',');
    for (var i = 0; i < HOUR_RANGES.length; i++) {
      if (HOUR_RANGES[i][0].test(genres)) {
        lo = HOUR_RANGES[i][1];
        hi = HOUR_RANGES[i][2];
        break;
      }
    }
    /* Games got longer. Without this a 1994 Metroidvania comes out at 47
       hours, which anyone who has played it will spot immediately. */
    var era = game.year < 2000 ? 0.55 : (game.year < 2010 ? 0.8 : 1);
    return Math.max(2, Math.round((lo + r() * (hi - lo)) * era));
  }

  var seedLogs = [];
  var dropped = [];
  ENTRIES.forEach(function (e, i) {
    var game = byTitle[e[1].toLowerCase()];
    if (!game) { dropped.push(e[1]); return; }
    var id = 'seed-' + i;
    var key = e[0] + '::' + game.id;
    var r = rng(hash32(key));
    seedLogs.push({
      id: id,
      user: e[0],
      game: game.id,
      rating: e[2],
      liked: !!e[3],
      date: e[4],
      review: e[5],
      replay: false,
      status: SEED_STATE[key] || 'finished',
      hours: seedHours(game, r),
      platform: game.platforms[Math.floor(r() * game.platforms.length)],
      likes: e[5] ? 4 + (hash32(id + e[1]) % 214) : 0,
      seeded: true
    });
  });
  if (dropped.length && window.console) {
    console.warn('Postgame: seeded entries with unknown titles', dropped);
  }

  /* ------------------------------------------------------ seeded discussion */

  /* [commenter, whose review, game title, date, body]. Referenced by author and
     title rather than by seed id, because the ids are array positions and would
     silently point at the wrong review the moment ENTRIES is reordered. */
  var COMMENTS = [
    ['quinnbyte', 'pixelvagrant', 'Elden Ring', '2022-03-21', 'This is the thing nobody warned me about. I went north out of Limgrave on hour two and did not see the Academy for another forty.'],
    ['neonharbor', 'pixelvagrant', 'Elden Ring', '2022-03-22', 'The map markers being player-placed is the whole design in one feature. You are never told, you are only ever hinted at.'],
    ['pixelvagrant', 'pixelvagrant', 'Elden Ring', '2022-03-22', '@neonharbor exactly. I found the Siofra well by falling down it.'],
    ['hollowbee', 'pixelvagrant', 'Elden Ring', '2022-04-02', 'Four hours is nothing, I have a 200 hour save where I still have not touched Caelid on purpose.'],

    ['glitchwitch', 'pixelvagrant', 'Sekiro: Shadows Die Twice', '2023-01-10', 'The moment it clicks is so specific. For me it was Genichiro phase two, suddenly the whole game reorganised itself.'],
    ['framerate_fran', 'pixelvagrant', 'Sekiro: Shadows Die Twice', '2023-01-11', 'It is a fighting game with one matchup and I mean that as the highest compliment. Deflect timing is frame data.'],
    ['pixelvagrant', 'pixelvagrant', 'Sekiro: Shadows Die Twice', '2023-01-12', '@framerate_fran that reframing is going to live in my head. It really is just neutral.'],

    ['sixteenbit', 'pixelvagrant', 'Dark Souls', '2021-11-04', 'The elevator is the single best level design moment of that generation and it is not close.'],
    ['neonharbor', 'pixelvagrant', 'Dark Souls', '2021-11-05', 'Everything after Anor Londo is a different, worse game though. Lost Izalith is where the origami runs out of paper.'],
    ['pixelvagrant', 'pixelvagrant', 'Dark Souls', '2021-11-05', '@neonharbor I will not be taking questions about the second half.'],

    ['moonlitmage', 'quinnbyte', "Baldur's Gate 3", '2023-09-02', 'Ninety hours is a speedrun. I am 140 in and still in act three.'],
    ['couchcoop_kev', 'quinnbyte', "Baldur's Gate 3", '2023-09-04', 'We played this four-player over six months. One friend spent the entire campaign as a bear on purpose.'],
    ['quinnbyte', 'quinnbyte', "Baldur's Gate 3", '2023-09-05', '@couchcoop_kev the bear was the correct choice and I will hear nothing else.'],
    ['neonharbor', 'quinnbyte', "Baldur's Gate 3", '2023-09-11', 'What gets me is that the failure states are all still content. Nothing punishes you with a reload.'],

    ['pixelvagrant', 'quinnbyte', 'Balatro', '2024-03-02', 'I have lost a weekend to this and I do not even like poker.'],
    ['glitchwitch', 'quinnbyte', 'Balatro', '2024-03-03', 'The run where you realise you can go infinite is genuinely dangerous. I saw 4am.'],
    ['hollowbee', 'quinnbyte', 'Balatro', '2024-03-09', 'Nobody tell me what a Blueprint does, I am figuring it out myself.'],
    ['quinnbyte', 'quinnbyte', 'Balatro', '2024-03-10', '@hollowbee you are going to be so annoyed when you find out.'],

    ['sixteenbit', 'quinnbyte', 'Red Dead Redemption 2', '2019-01-15', 'The slow part is the point and it took me two attempts to understand that. First time I bounced hard.'],
    ['moonlitmage', 'quinnbyte', 'Red Dead Redemption 2', '2019-02-01', 'Riding back from Guarma in silence is the best hour in the game and nothing happens in it.'],

    ['pixelvagrant', 'neonharbor', 'Dishonored 2', '2016-11-24', 'Six ways is not enough. The Clockwork Mansion is the only level I have ever mapped on paper.'],
    ['quinnbyte', 'neonharbor', 'Dishonored 2', '2016-11-25', 'Crack in the Slab does the same trick with time and somehow they put both in one game.'],
    ['neonharbor', 'neonharbor', 'Dishonored 2', '2016-11-26', '@quinnbyte Crack in the Slab is the better level and the Mansion is the better idea. I go back and forth.'],

    ['hollowbee', 'neonharbor', 'Outer Wilds', '2020-01-22', 'The way you recommend this game is by saying nothing and watching them suffer for two weeks.'],
    ['moonlitmage', 'neonharbor', 'Outer Wilds', '2020-02-03', 'I have never wanted to erase my memory of something so badly.'],
    ['glitchwitch', 'neonharbor', 'Outer Wilds', '2020-02-14', 'Knowledge as the only upgrade means the credits roll on you, not the character. Still thinking about it years later.'],
    ['neonharbor', 'neonharbor', 'Outer Wilds', '2020-02-15', '@glitchwitch that is a better way of putting it than anything in my review.'],

    ['neonharbor', 'sixteenbit', 'Super Metroid', '2015-02-22', 'Every studio doing "environmental storytelling" in a pitch deck should be made to replay the first ten minutes of this.'],
    ['hollowbee', 'sixteenbit', 'Super Metroid', '2015-03-01', 'The wall jump being taught by animals instead of a tooltip. Thirty years and nobody has improved on it.'],
    ['sixteenbit', 'sixteenbit', 'Super Metroid', '2015-03-02', '@hollowbee and you can skip the lesson entirely if you already know. That is the respect part.'],

    ['framerate_fran', 'sixteenbit', 'Tekken 3', '2018-04-14', 'Still the cleanest the series has ever felt. Everything since has more systems and less clarity.'],
    ['couchcoop_kev', 'sixteenbit', 'GoldenEye 007', '2014-12-26', 'Oddjob ban is the oldest house rule in gaming and it is still enforced at mine.'],
    ['sixteenbit', 'sixteenbit', 'GoldenEye 007', '2014-12-27', '@couchcoop_kev anyone who argues gets the controller with the broken C-buttons.'],

    ['quinnbyte', 'haloweenie', 'Titanfall 2', '2017-01-25', 'Effect and Cause is the best single level of that decade and it was in a game nobody played.'],
    ['pixelvagrant', 'haloweenie', 'Titanfall 2', '2017-02-01', 'Released between Battlefield 1 and Infinite Warfare. EA buried it and then acted surprised.'],
    ['haloweenie', 'haloweenie', 'Titanfall 2', '2017-02-02', '@pixelvagrant I am still annoyed about it in 2017 and I will be annoyed about it in 2027.'],
    ['glitchwitch', 'haloweenie', 'Titanfall 2', '2018-06-30', 'The movement tech ceiling is absurd too. Watch any wall-run speedrun and it stops looking like the same game.'],

    ['couchcoop_kev', 'haloweenie', 'Halo 3', '2010-08-12', 'Four of us on one couch, split screen, all summer. No game has replaced that and none of them have tried.'],
    ['sixteenbit', 'haloweenie', 'Halo 3', '2010-08-20', 'Forge turned a shooter into a toy box two years before anyone used the phrase user generated content.'],

    ['pixelvagrant', 'moonlitmage', 'Nier: Automata', '2018-03-25', 'Ending E is the only time a game has asked me for something real and I gave it up without thinking.'],
    ['hollowbee', 'moonlitmage', 'Nier: Automata', '2018-04-02', 'I will not read this thread. I am on route B. Nobody say anything.'],
    ['moonlitmage', 'moonlitmage', 'Nier: Automata', '2018-04-03', '@hollowbee correct instinct. Come back in a month.'],

    ['quinnbyte', 'moonlitmage', 'Persona 5 Royal', '2020-04-08', 'The confidants being the actual game is right, and it is also why the dungeon crawling drags for some people.'],
    ['moonlitmage', 'moonlitmage', 'Persona 5 Royal', '2020-04-09', '@quinnbyte Mementos is the price of admission. Everything above ground is worth it.'],
    ['glitchwitch', 'moonlitmage', 'Clair Obscur: Expedition 33', '2025-05-09', 'Parry timing in a turn-based game should not work and yet here we are. It fixed the genre for me.'],

    ['pixelvagrant', 'hollowbee', 'Hollow Knight', '2019-05-02', 'Greenpath into Fog Canyon is one of the great smooth difficulty curves. You never notice it happening.'],
    ['glitchwitch', 'hollowbee', 'Hollow Knight', '2019-05-14', 'Path of Pain nearly ended me. Worth it for a cutscene most people never see.'],
    ['hollowbee', 'hollowbee', 'Hollow Knight', '2019-05-15', '@glitchwitch Path of Pain is the real final boss and the actual final boss knows it.'],

    ['couchcoop_kev', 'framerate_fran', 'Street Fighter 6', '2023-06-10', 'Drive Rush made this watchable for people who do not play fighting games. That is a real achievement.'],
    ['framerate_fran', 'framerate_fran', 'Street Fighter 6', '2023-06-11', '@couchcoop_kev modern controls did more for the scene than a decade of tutorials.']
  ];

  var seedLogByKey = Object.create(null);
  seedLogs.forEach(function (l) { seedLogByKey[l.user + '::' + l.game] = l.id; });

  var seedComments = [];
  var lostComments = [];
  COMMENTS.forEach(function (c, i) {
    var game = byTitle[c[2].toLowerCase()];
    var logId = game ? seedLogByKey[c[1] + '::' + game.id] : null;
    /* A comment with no review to hang off is a typo in the table above, not a
       runtime condition — say so rather than dropping it silently. */
    if (!logId) { lostComments.push(c[1] + ' on ' + c[2]); return; }
    seedComments.push({
      id: 'sc-' + i,
      log: logId,
      user: c[0],
      date: c[3],
      body: c[4],
      seeded: true
    });
  });
  if (lostComments.length && window.console) {
    console.warn('Postgame: seeded comments with no matching review', lostComments);
  }

  /* ------------------------------------------------------------- who follows */

  /* Clustered by taste rather than wired at random, so the "Following" feed
     actually looks like someone's feed: the Souls player sees the metroidvania
     and roguelike people, the retro collector sees the fighting-game one.
     Deliberately lopsided — glitchwitch follows six and is followed by two. */
  var FOLLOWS = {
    pixelvagrant: ['hollowbee', 'glitchwitch', 'neonharbor', 'framerate_fran'],
    quinnbyte: ['couchcoop_kev', 'moonlitmage', 'pixelvagrant', 'haloweenie'],
    neonharbor: ['pixelvagrant', 'hollowbee', 'glitchwitch'],
    sixteenbit: ['framerate_fran', 'hollowbee', 'couchcoop_kev'],
    haloweenie: ['couchcoop_kev', 'quinnbyte', 'framerate_fran'],
    moonlitmage: ['hollowbee', 'quinnbyte', 'neonharbor'],
    framerate_fran: ['sixteenbit', 'pixelvagrant', 'glitchwitch'],
    couchcoop_kev: ['quinnbyte', 'haloweenie', 'sixteenbit', 'moonlitmage'],
    hollowbee: ['pixelvagrant', 'sixteenbit', 'glitchwitch', 'neonharbor'],
    glitchwitch: ['pixelvagrant', 'hollowbee', 'framerate_fran', 'sixteenbit',
      'neonharbor', 'moonlitmage']
  };

  var known = Object.create(null);
  MEMBERS.forEach(function (m) { known[m.username] = 1; });

  var seedFollows = Object.create(null);
  var strayFollows = [];
  Object.keys(FOLLOWS).forEach(function (who) {
    if (!known[who]) { strayFollows.push(who); return; }
    seedFollows[who] = FOLLOWS[who].filter(function (target) {
      /* A typo here would quietly create a follow of nobody, which then shows
         up as a broken link in someone's following list. */
      if (!known[target] || target === who) {
        strayFollows.push(who + ' -> ' + target);
        return false;
      }
      return true;
    });
  });
  if (strayFollows.length && window.console) {
    console.warn('Postgame: seeded follows pointing at unknown members', strayFollows);
  }

  /* ------------------------------------------------------------- messages */

  /* [from, to, sent, body]. Every pair here is a mutual follow in FOLLOWS
     above — the same rule the compose box enforces — so the seeded inbox could
     not have been written by a route the UI does not allow. */
  var MESSAGES = [
    ['hollowbee', 'pixelvagrant', '2026-06-02T19:04', 'Did you get to the Citadel in Silksong yet? I need someone to be annoyed with.'],
    ['pixelvagrant', 'hollowbee', '2026-06-02T19:41', 'Two nights on the bell trap. I have opinions and none of them are kind.'],
    ['hollowbee', 'pixelvagrant', '2026-06-02T19:55', 'Good. Wait until the roof section.'],
    ['pixelvagrant', 'hollowbee', '2026-06-03T08:12', 'You could have warned me.'],
    ['hollowbee', 'pixelvagrant', '2026-06-03T08:30', 'I did. You called it "a skill issue" and hung up.'],
    ['pixelvagrant', 'hollowbee', '2026-06-03T09:02', 'That does sound like me.'],
    ['hollowbee', 'pixelvagrant', '2026-07-28T17:20', 'Nine Sols is on sale and it is basically Sekiro. You have no excuse now.'],

    ['pixelvagrant', 'glitchwitch', '2026-05-14T21:10', 'How many Hades II runs are you on? Be honest.'],
    ['glitchwitch', 'pixelvagrant', '2026-05-14T21:26', 'I am not going to answer that in writing.'],
    ['pixelvagrant', 'glitchwitch', '2026-05-14T21:31', 'That is an answer.'],
    ['glitchwitch', 'pixelvagrant', '2026-05-15T00:48', '84. Melinoe is meaner than Zag and I like her more for it.'],
    ['pixelvagrant', 'glitchwitch', '2026-05-15T09:15', 'Sent at quarter to one in the morning. Roguelike brain.'],
    ['glitchwitch', 'pixelvagrant', '2026-07-30T23:12', 'New Balatro seeded run tomorrow if you want to watch someone lose their mind.'],

    ['neonharbor', 'pixelvagrant', '2026-04-09T12:00', 'You would like Dishonored 2 more than you think. It is a parry game if you play it wrong enough.'],
    ['pixelvagrant', 'neonharbor', '2026-04-09T12:34', 'Everything is a parry game if I play it wrong enough.'],
    ['neonharbor', 'pixelvagrant', '2026-04-09T12:40', 'Clockwork Mansion. One sitting. Report back.'],
    ['pixelvagrant', 'neonharbor', '2026-04-11T22:05', 'Finished it. The mansion is the best level I have played in years and I am irritated about it.'],
    ['neonharbor', 'pixelvagrant', '2026-04-11T22:19', 'That is the correct reaction.'],

    ['framerate_fran', 'pixelvagrant', '2026-03-02T15:44', 'Sekiro deflect timing is 30 frames on the loosest setting. You are playing a fighting game and you do not know it.'],
    ['pixelvagrant', 'framerate_fran', '2026-03-02T16:02', 'I have been told. I refuse to learn frame data on principle.'],
    ['framerate_fran', 'pixelvagrant', '2026-03-02T16:09', 'The principle is losing.'],

    ['couchcoop_kev', 'quinnbyte', '2026-07-01T18:30', 'Split Fiction on Saturday? I will bring the second controller and the poor decisions.'],
    ['quinnbyte', 'couchcoop_kev', '2026-07-01T18:52', 'Only if we finish it this time. Four sessions is not a co-op game, it is a relationship.'],
    ['couchcoop_kev', 'quinnbyte', '2026-07-01T19:00', 'We finished It Takes Two eventually.'],
    ['quinnbyte', 'couchcoop_kev', '2026-07-01T19:04', 'In eleven months.'],

    ['sixteenbit', 'framerate_fran', '2026-02-18T20:15', 'Tekken 3 still feels more correct than anything since and I will die on this hill.'],
    ['framerate_fran', 'sixteenbit', '2026-02-18T20:40', 'Tekken 8 has better neutral and you know it. The hill is not defensible.'],
    ['sixteenbit', 'framerate_fran', '2026-02-18T20:51', 'Better neutral, worse feel. Both can be true.'],
    ['framerate_fran', 'sixteenbit', '2026-02-18T21:02', 'Annoyingly, yes.']
  ];

  var seedMessages = [];
  var strayMessages = [];
  MESSAGES.forEach(function (m, i) {
    if (!known[m[0]] || !known[m[1]] || m[0] === m[1]) {
      strayMessages.push(m[0] + ' -> ' + m[1]);
      return;
    }
    seedMessages.push({
      id: 'sm-' + i, from: m[0], to: m[1], sent: m[2], body: m[3], seeded: true
    });
  });
  if (strayMessages.length && window.console) {
    console.warn('Postgame: seeded messages with unknown members', strayMessages);
  }

  /* Where each member had read up to before this visit. Without a baseline the
     whole seeded inbox counts as unread and the badge opens on "12", which
     reads as a bug rather than as a demo. These leave a couple genuinely
     unread — the most recent line in two of pixelvagrant's threads. */
  var seedReads = [
    ['pixelvagrant', 'hollowbee', '2026-06-03T09:00'],
    ['pixelvagrant', 'glitchwitch', '2026-05-15T10:00'],
    ['pixelvagrant', 'neonharbor', '2026-04-12T00:00'],
    ['pixelvagrant', 'framerate_fran', '2026-03-03T00:00'],
    ['hollowbee', 'pixelvagrant', '2026-06-03T10:00'],
    ['glitchwitch', 'pixelvagrant', '2026-05-15T10:00'],
    ['neonharbor', 'pixelvagrant', '2026-04-12T00:00'],
    ['framerate_fran', 'pixelvagrant', '2026-03-03T00:00'],
    ['quinnbyte', 'couchcoop_kev', '2026-07-01T20:00'],
    ['couchcoop_kev', 'quinnbyte', '2026-07-01T20:00'],
    ['sixteenbit', 'framerate_fran', '2026-02-18T22:00'],
    ['framerate_fran', 'sixteenbit', '2026-02-18T22:00']
  ];

  /* Give every demo member a small "want to play" list drawn from games they
     have not logged, so the shelves are not empty on a fresh visit. */
  var seedBacklog = Object.create(null);
  MEMBERS.forEach(function (m) {
    var logged = Object.create(null);
    seedLogs.forEach(function (l) { if (l.user === m.username) logged[l.game] = 1; });
    var r = rng(hash32(m.username));
    var picks = [];
    var guard = 0;
    while (picks.length < 6 && guard < 400) {
      guard++;
      var g = games[Math.floor(r() * 140)];
      if (!g || logged[g.id] || picks.indexOf(g.id) > -1) continue;
      picks.push(g.id);
    }
    seedBacklog[m.username] = picks;
  });

  /* ------------------------------------------------------------------ misc */

  function trending(count) {
    /* A stable weekly shuffle of the most-followed games. */
    var pool = games.slice(0, 120);
    var r = rng(20260730);
    var arr = pool.slice();
    for (var i = arr.length - 1; i > 0; i--) {
      var j = Math.floor(r() * (i + 1));
      var t = arr[i]; arr[i] = arr[j]; arr[j] = t;
    }
    return arr.slice(0, count);
  }

  PG.util = { slugify: slugify, hash32: hash32, rng: rng };
  PG.data = {
    games: games,
    byId: byId,
    byTitle: byTitle,
    genres: genres,
    platforms: platforms,
    decades: decades,
    buckets: BUCKETS,
    search: search,
    trending: trending,
    members: MEMBERS,
    seedLogs: seedLogs,
    seedComments: seedComments,
    seedFollows: seedFollows,
    seedMessages: seedMessages,
    seedReads: seedReads,
    seedBacklog: seedBacklog
  };
})(window.PG);
