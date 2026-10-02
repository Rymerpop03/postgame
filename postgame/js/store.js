/* Postgame — accounts and persistence
   Everything lives in localStorage. These are demo accounts, not real ones:
   there is no server, no encryption and no privacy boundary here. */
window.PG = window.PG || {};
(function (PG) {
  'use strict';

  var KEY = 'postgame.v1';
  var DEMO_PASSWORD = 'postgame';

  var state = null;

  function blank() {
    return {
      users: [], session: null, logs: [], backlog: {}, reviewLikes: {},
      /* Comments written here, plus the ids of seeded ones that have been
         deleted — the seeded table itself is rebuilt from data.js each load. */
      comments: [], hiddenComments: {},
      /* username -> { add: [], remove: [] } over the seeded follow graph, the
         same overlay shape the backlog uses and for the same reason. */
      following: {},
      /* Direct messages written here, plus "reader::other" -> ISO timestamp of
         how far each member has read in each conversation. */
      messages: [], reads: {},
      /* username -> edited { name, bio, hue, avatar }. An override layer rather
         than a field on the record, because the demo members live in data.js
         and are rebuilt from source on every load. */
      profiles: {}
    };
  }

  function load() {
    if (state) return state;
    try {
      var raw = window.localStorage.getItem(KEY);
      state = raw ? JSON.parse(raw) : blank();
    } catch (e) {
      state = blank();
    }
    var b = blank();
    Object.keys(b).forEach(function (k) {
      if (state[k] === undefined || state[k] === null) state[k] = b[k];
    });
    return state;
  }

  /* Returns false when the write was rejected — almost always the storage quota,
     which a profile picture is the only thing here big enough to hit. Callers
     that can do something useful about it check; the rest ignore it. */
  function save() {
    logCache = null;
    commentCache = null;
    genreCache = null;
    messageCache = null;
    try {
      window.localStorage.setItem(KEY, JSON.stringify(load()));
      return true;
    } catch (e) {
      if (window.console) console.warn('Postgame: could not save locally', e);
      return false;
    }
  }

  /* -------------------------------------------------------------- accounts */

  /* Lays any saved profile edits over a member record. Returns a copy, so the
     seeded objects in data.js are never mutated, and leaves fields the edit
     does not mention alone — including `pw` and `seeded`, which login reads. */
  function decorate(member) {
    if (!member) return null;
    var patch = load().profiles[member.username.toLowerCase()];
    var out = {};
    Object.keys(member).forEach(function (k) { out[k] = member[k]; });
    if (patch) {
      Object.keys(patch).forEach(function (k) {
        if (patch[k] !== undefined && patch[k] !== null) out[k] = patch[k];
      });
    }
    /* Censored here rather than at each place a name or bio is drawn. Those
       two fields surface in a dozen components — profile headers, member cards,
       review bylines, the message list, search — and masking at the single
       point they are read from is the only version of this that cannot be
       missed when a new component is added. */
    out.name = PG.moderate.mask(out.name);
    if (out.bio) out.bio = PG.moderate.mask(out.bio);
    return out;
  }

  function findMember(username) {
    var u = String(username || '').toLowerCase();
    var seeded = PG.data.members.filter(function (m) {
      return m.username.toLowerCase() === u;
    })[0];
    if (seeded) return decorate(seeded);
    return decorate(load().users.filter(function (m) {
      return m.username.toLowerCase() === u;
    })[0]) || null;
  }

  function allMembers() {
    return PG.data.members.concat(load().users).map(decorate);
  }

  var NAME_MAX = 40;
  var BIO_MAX = 240;

  /* Editing your own display name, bio, avatar colour or picture. Anything
     omitted from the patch keeps its current value, so the same call can save
     one field or all four. */
  function updateProfile(patch) {
    var me = currentUser();
    if (!me) return { error: 'Sign in to edit your profile.' };

    var next = {};

    if (patch.name !== undefined) {
      var name = String(patch.name).trim();
      if (!name) return { error: 'Your display name cannot be empty.' };
      if (name.length > NAME_MAX) {
        return { error: 'Display names are ' + NAME_MAX + ' characters or fewer.' };
      }
      if (!PG.moderate.checkName(name).clean) {
        PG.moderate.recordBlock();
        return { error: PG.moderate.message };
      }
      next.name = name;
    }

    if (patch.bio !== undefined) {
      var bio = String(patch.bio).trim();
      if (bio.length > BIO_MAX) {
        return { error: 'Bios are ' + BIO_MAX + ' characters or fewer.' };
      }
      if (!PG.moderate.check(bio).clean) {
        PG.moderate.recordBlock();
        return { error: PG.moderate.message };
      }
      next.bio = bio;
    }

    if (patch.hue !== undefined) {
      var hue = Math.round(Number(patch.hue));
      if (isNaN(hue)) return { error: 'That is not a colour we recognise.' };
      next.hue = ((hue % 360) + 360) % 360;
    }

    /* '' clears the picture and falls back to the lettered avatar. Only data
       URLs are accepted: the value goes straight into a CSS url(), and there is
       no server here to host anything else. */
    if (patch.avatar !== undefined) {
      var avatar = String(patch.avatar || '');
      if (avatar && !/^data:image\/(png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+$/.test(avatar)) {
        return { error: 'That image could not be read.' };
      }
      next.avatar = avatar;
    }

    var all = load().profiles;
    var key = me.username.toLowerCase();
    var record = all[key] || (all[key] = {});
    var before = {};
    Object.keys(record).forEach(function (k) { before[k] = record[k]; });
    Object.keys(next).forEach(function (k) { record[k] = next[k]; });

    /* Put the old values back if the write was refused, so the page cannot show
       a picture that will not survive a reload. */
    if (!save()) {
      all[key] = before;
      save();
      return {
        error: 'No room left in this browser\'s storage. Try a smaller picture.'
      };
    }
    return { user: currentUser() };
  }

  /* Not a security measure — just avoids storing the literal string. */
  function scramble(pw) {
    return 'h' + PG.util.hash32('postgame::' + pw).toString(36);
  }

  function register(username, displayName, password) {
    username = String(username || '').trim();
    displayName = String(displayName || '').trim() || username;
    if (!/^[a-zA-Z0-9_]{3,20}$/.test(username)) {
      return { error: 'Usernames are 3 to 20 characters: letters, numbers and underscores.' };
    }
    if (String(password || '').length < 4) {
      return { error: 'Use at least 4 characters for the password.' };
    }
    if (findMember(username)) {
      return { error: 'That username is already taken.' };
    }
    /* The handle matters as much as the display name — it is in every URL and
       every byline on the site. */
    if (!PG.moderate.checkName(username).clean ||
        !PG.moderate.checkName(displayName).clean) {
      PG.moderate.recordBlock();
      return { error: PG.moderate.message };
    }
    var user = {
      username: username,
      name: displayName,
      hue: PG.util.hash32(username) % 360,
      bio: '',
      pw: scramble(password),
      joined: new Date().toISOString().slice(0, 10)
    };
    load().users.push(user);
    load().session = user.username;
    save();
    return { user: user };
  }

  function login(username, password) {
    var member = findMember(username);
    if (!member) return { error: 'No account with that username.' };
    var expected = member.seeded ? scramble(DEMO_PASSWORD) : member.pw;
    if (scramble(password) !== expected) return { error: 'Incorrect password.' };
    load().session = member.username;
    save();
    return { user: member };
  }

  function logout() {
    load().session = null;
    save();
  }

  function currentUser() {
    var s = load().session;
    return s ? findMember(s) : null;
  }

  /* ------------------------------------------------------------------ logs */

  /* Seeded diary entries are constant; anything the signed-in member writes is
     stored locally and takes precedence for the same (member, game) pair.
     The merged list is rebuilt only after a write — sorting 500 games by
     community score reads it once per game. */
  var logCache = null;

  function allLogs() {
    if (logCache) return logCache;
    var own = load().logs;
    var overridden = Object.create(null);
    own.forEach(function (l) { overridden[l.user + '::' + l.game] = 1; });
    var seeded = PG.data.seedLogs.filter(function (l) {
      return !overridden[l.user + '::' + l.game];
    });
    logCache = seeded.concat(own.filter(function (l) { return !l.deleted; }));
    return logCache;
  }

  function logsFor(gameId) {
    return allLogs().filter(function (l) { return l.game === gameId; });
  }

  function logsBy(username) {
    return allLogs().filter(function (l) { return l.user === username; });
  }

  function myLog(gameId) {
    var me = currentUser();
    if (!me) return null;
    return allLogs().filter(function (l) {
      return l.game === gameId && l.user === me.username;
    })[0] || null;
  }

  /* Entries written before play states existed, and every seeded one that is
     not mid-playthrough, read as finished. */
  var STATES = ['playing', 'finished', 'abandoned'];

  function statusOf(log) {
    if (!log) return 'finished';
    return STATES.indexOf(log.status) > -1 ? log.status : 'finished';
  }

  function today() {
    return new Date().toISOString().slice(0, 10);
  }

  /* Long-form, but bounded. Everything else a member can type has a ceiling on
     both the control and the write; reviews had neither, which put an unbounded
     string into a 5MB storage quota shared with the cover-art cache. */
  var REVIEW_MAX = 6000;

  function fmtCount(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function saveLog(entry) {
    var me = currentUser();
    if (!me) return { error: 'Sign in to log a game.' };
    if (entry.review && String(entry.review).length > REVIEW_MAX) {
      return {
        error: 'That review is longer than ' + fmtCount(REVIEW_MAX) +
          ' characters. Trim it down a little.'
      };
    }
    if (entry.review && !PG.moderate.check(entry.review).clean) {
      PG.moderate.recordBlock();
      return { error: PG.moderate.message };
    }
    var own = load().logs;
    var existing = own.filter(function (l) {
      return l.user === me.username && l.game === entry.game;
    })[0];
    /* Editing one of a demo member's seeded entries writes a local record over
       it — carry the like count across so the review does not lose its votes. */
    var seeded = PG.data.seedLogs.filter(function (l) {
      return l.user === me.username && l.game === entry.game;
    })[0];
    var record = existing || {
      id: 'u-' + Date.now().toString(36) + Math.floor(Math.random() * 1e4).toString(36),
      user: me.username,
      game: entry.game,
      likes: seeded ? seeded.likes : 0
    };
    record.rating = entry.rating;
    record.liked = !!entry.liked;
    record.review = entry.review || '';
    record.date = entry.date || today();
    record.replay = !!entry.replay;
    record.status = STATES.indexOf(entry.status) > -1 ? entry.status : 'finished';
    record.hours = entry.hours === '' || entry.hours === null ||
      entry.hours === undefined || isNaN(Number(entry.hours)) ?
      null : Math.max(0, Math.round(Number(entry.hours) * 10) / 10);
    record.platform = entry.platform || '';
    record.deleted = false;
    record.edited = new Date().toISOString();
    if (!existing) own.push(record);
    /* Logging a game removes it from the want-to-play shelf. */
    setBacklog(entry.game, false, true);
    save();
    return { log: record };
  }

  /* Changing one field from outside the log form — rating straight from a
     poster, or flipping a game to "playing" — without disturbing the rest of
     the entry, and creating one if there isn't one yet. */
  function patchLog(gameId, patch) {
    var me = currentUser();
    if (!me) return { error: 'Sign in to log a game.' };
    var prior = myLog(gameId);
    var next = {
      game: gameId,
      rating: prior ? prior.rating : null,
      liked: prior ? prior.liked : false,
      replay: prior ? prior.replay : false,
      review: prior ? prior.review : '',
      date: prior && prior.date ? prior.date : today(),
      status: statusOf(prior),
      hours: prior ? prior.hours : null,
      platform: prior ? prior.platform : ''
    };
    Object.keys(patch).forEach(function (k) { next[k] = patch[k]; });
    return saveLog(next);
  }

  function quickRate(gameId, rating) {
    return patchLog(gameId, { rating: rating });
  }

  function quickState(gameId, status) {
    return patchLog(gameId, { status: status });
  }

  function removeLog(gameId) {
    var me = currentUser();
    if (!me) return;
    var own = load().logs;
    var existing = own.filter(function (l) {
      return l.user === me.username && l.game === gameId;
    })[0];
    if (existing) {
      existing.deleted = true;
      existing.review = '';
      existing.rating = null;
    } else {
      /* Hide a seeded entry by writing a deleted stub over it. */
      own.push({ id: 'u-' + Date.now().toString(36), user: me.username, game: gameId, deleted: true });
    }
    save();
  }

  /* --------------------------------------------------------------- backlog */

  function backlogFor(username) {
    var seeded = PG.data.seedBacklog[username] || [];
    var mine = load().backlog[username] || { add: [], remove: [] };
    var out = seeded.filter(function (id) { return mine.remove.indexOf(id) === -1; });
    mine.add.forEach(function (id) { if (out.indexOf(id) === -1) out.push(id); });
    return out;
  }

  function inBacklog(gameId) {
    var me = currentUser();
    return !!me && backlogFor(me.username).indexOf(gameId) > -1;
  }

  function setBacklog(gameId, wanted, quiet) {
    var me = currentUser();
    if (!me) return { error: 'Sign in to keep a want-to-play list.' };
    var all = load().backlog;
    var mine = all[me.username] || (all[me.username] = { add: [], remove: [] });
    function pull(arr, v) { var i = arr.indexOf(v); if (i > -1) arr.splice(i, 1); }
    if (wanted) {
      pull(mine.remove, gameId);
      if (mine.add.indexOf(gameId) === -1) mine.add.push(gameId);
    } else {
      pull(mine.add, gameId);
      if (mine.remove.indexOf(gameId) === -1) mine.remove.push(gameId);
    }
    if (!quiet) save();
    return { wanted: !!wanted };
  }

  /* ------------------------------------------------------------- following */

  function followingOf(username) {
    var seeded = PG.data.seedFollows[username] || [];
    var mine = load().following[username] || { add: [], remove: [] };
    var out = seeded.filter(function (u) { return mine.remove.indexOf(u) === -1; });
    mine.add.forEach(function (u) { if (out.indexOf(u) === -1) out.push(u); });
    return out;
  }

  /* No reverse index: the follow graph is one row per member, so working it out
     on demand costs a walk of ten-ish lists and never goes stale. */
  function followersOf(username) {
    return allMembers().filter(function (m) {
      return followingOf(m.username).indexOf(username) > -1;
    }).map(function (m) { return m.username; });
  }

  function followCounts(username) {
    return {
      following: followingOf(username).length,
      followers: followersOf(username).length
    };
  }

  function isFollowing(target) {
    var me = currentUser();
    return !!me && followingOf(me.username).indexOf(target) > -1;
  }

  function setFollowing(target, wanted) {
    var me = currentUser();
    if (!me) return { error: 'Sign in to follow other members.' };
    if (!findMember(target)) return { error: 'No member with that name.' };
    if (target === me.username) return { error: 'You cannot follow yourself.' };
    var all = load().following;
    var mine = all[me.username] || (all[me.username] = { add: [], remove: [] });
    function pull(arr, v) { var i = arr.indexOf(v); if (i > -1) arr.splice(i, 1); }
    if (wanted) {
      pull(mine.remove, target);
      if (mine.add.indexOf(target) === -1) mine.add.push(target);
    } else {
      pull(mine.add, target);
      if (mine.remove.indexOf(target) === -1) mine.remove.push(target);
    }
    save();
    return { following: !!wanted };
  }

  function toggleFollow(target) {
    return setFollowing(target, !isFollowing(target));
  }

  /* Diary entries from the people a member follows. Their own entries stay out:
     the point of the feed is what everyone else is playing. */
  function logsFromFollowing(username) {
    var circle = Object.create(null);
    followingOf(username).forEach(function (u) { circle[u] = 1; });
    return allLogs().filter(function (l) { return circle[l.user]; });
  }

  /* -------------------------------------------------------------- messages */

  var MESSAGE_MAX = 1000;
  var messageCache = null;

  /* Messaging needs consent from both sides, so the gate is a mutual follow —
     following someone does not entitle you to their inbox. */
  function canMessage(a, b) {
    if (!a || !b || a === b) return false;
    return followingOf(a).indexOf(b) > -1 && followingOf(b).indexOf(a) > -1;
  }

  function allMessages() {
    if (messageCache) return messageCache;
    messageCache = PG.data.seedMessages.concat(
      load().messages.filter(function (m) { return !m.deleted; }));
    return messageCache;
  }

  function bySent(a, b) {
    return String(a.sent).localeCompare(String(b.sent)) ||
      String(a.id).localeCompare(String(b.id));
  }

  /* One conversation, oldest first — it reads like a chat log, not a feed. */
  function messagesWith(other, username) {
    var me = username || (currentUser() || {}).username;
    if (!me || !other) return [];
    return allMessages().filter(function (m) {
      return (m.from === me && m.to === other) || (m.from === other && m.to === me);
    }).sort(bySent);
  }

  function readKey(reader, other) { return reader + '::' + other; }

  function lastReadAt(reader, other) {
    var stored = load().reads[readKey(reader, other)];
    if (stored) return stored;
    var seeded = PG.data.seedReads.filter(function (r) {
      return r[0] === reader && r[1] === other;
    })[0];
    return seeded ? seeded[2] : '';
  }

  function unreadWith(other, username) {
    var me = username || (currentUser() || {}).username;
    if (!me) return 0;
    var since = lastReadAt(me, other);
    return messagesWith(other, me).filter(function (m) {
      return m.to === me && String(m.sent) > since;
    }).length;
  }

  function markRead(other) {
    var me = currentUser();
    if (!me || !other) return;
    var thread = messagesWith(other, me.username);
    if (!thread.length) return;
    var newest = thread[thread.length - 1].sent;
    var key = readKey(me.username, other);
    if (load().reads[key] === newest) return;   /* nothing moved; skip the write */
    load().reads[key] = newest;
    save();
  }

  /* Everyone the signed-in member has a thread with, newest activity first,
     plus anyone they could write to but never have. */
  function conversations(includeEmpty) {
    var me = currentUser();
    if (!me) return [];
    var others = Object.create(null);
    allMessages().forEach(function (m) {
      if (m.from === me.username) others[m.to] = 1;
      else if (m.to === me.username) others[m.from] = 1;
    });
    if (includeEmpty) {
      followingOf(me.username).forEach(function (u) {
        if (canMessage(me.username, u)) others[u] = others[u] || 1;
      });
    }
    return Object.keys(others).map(function (handle) {
      var thread = messagesWith(handle, me.username);
      var last = thread[thread.length - 1] || null;
      return {
        member: findMember(handle) ||
          { username: handle, name: handle, hue: 260 },
        last: last,
        unread: unreadWith(handle, me.username),
        open: canMessage(me.username, handle)
      };
    }).filter(function (c) {
      return !!c.member;
    }).sort(function (x, y) {
      /* Threads with something in them first, newest at the top; never-used
         ones fall to the bottom in name order. */
      if (!x.last && !y.last) return x.member.username.localeCompare(y.member.username);
      if (!x.last) return 1;
      if (!y.last) return -1;
      return String(y.last.sent).localeCompare(String(x.last.sent));
    });
  }

  function unreadTotal() {
    var me = currentUser();
    if (!me) return 0;
    var seen = Object.create(null);
    var n = 0;
    allMessages().forEach(function (m) {
      if (m.to !== me.username) return;
      if (!seen[m.from]) {
        seen[m.from] = unreadWith(m.from, me.username);
        n += seen[m.from];
      }
    });
    return n;
  }

  function sendMessage(to, body) {
    var me = currentUser();
    if (!me) return { error: 'Sign in to send a message.' };
    if (!findMember(to)) return { error: 'No member with that name.' };
    if (to === me.username) return { error: 'You cannot message yourself.' };
    if (!canMessage(me.username, to)) {
      return { error: 'You can only message members who follow you back.' };
    }
    var text = String(body || '').trim();
    if (!text) return { error: 'Write something first.' };
    if (text.length > MESSAGE_MAX) {
      return { error: 'Messages are ' + MESSAGE_MAX + ' characters or fewer.' };
    }
    if (!PG.moderate.check(text).clean) {
      PG.moderate.recordBlock();
      return { error: PG.moderate.message };
    }
    var message = {
      id: 'm-' + Date.now().toString(36) + Math.floor(Math.random() * 1e4).toString(36),
      from: me.username,
      to: to,
      sent: new Date().toISOString(),
      body: text
    };
    load().messages.push(message);
    messageCache = null;
    /* Sending catches you up on the thread — you have plainly seen it. */
    load().reads[readKey(me.username, to)] = message.sent;
    save();
    return { message: message };
  }

  /* ------------------------------------------------------------ taste match */

  /* A gap of this many stars or more reads as flat disagreement; anything
     smaller scales linearly. Half a star apart is near-agreement, two and a
     half apart is two people who did not play the same game. */
  var TASTE_GAP = 2.5;
  /* How many shared games it takes before rating agreement outweighs the genre
     prior. Below this the score leans on what they *choose* to play, which is
     the only signal available in a 2,000-game catalogue where two people can
     easily have nothing in common. */
  var TASTE_PRIOR = 4;
  /* Under this much overlap the score is honest about resting on genres. */
  var TASTE_MIN = 3;
  /* Ratings a member needs before their library is taken at face value. Without
     this a brand-new account that rates one metroidvania scores a 91% "strong
     match" with the metroidvania obsessive, because a one-game genre vector
     points in exactly one direction. Below the threshold the whole score is
     pulled toward the middle in proportion to how thin the thinner library is. */
  var TASTE_LIBRARY = 8;
  var TASTE_NEUTRAL = 0.5;

  var genreCache = null;

  function ratingsOf(username) {
    var out = Object.create(null);
    logsBy(username).forEach(function (l) {
      if (l.rating) out[l.game] = l.rating;
    });
    return out;
  }

  /* What a member reaches for, weighted by how much they liked it. Normalised
     by cosine below, so someone with 200 entries is comparable to someone with
     twelve. */
  function genreVector(username) {
    if (!genreCache) genreCache = Object.create(null);
    if (genreCache[username]) return genreCache[username];
    var v = Object.create(null);
    logsBy(username).forEach(function (l) {
      if (!l.rating) return;
      var game = PG.data.byId[l.game];
      if (!game) return;
      var w = l.rating / 5;
      game.genres.forEach(function (g) { v[g] = (v[g] || 0) + w; });
    });
    genreCache[username] = v;
    return v;
  }

  function cosine(a, b) {
    var dot = 0, na = 0, nb = 0;
    Object.keys(a).forEach(function (k) {
      na += a[k] * a[k];
      if (b[k]) dot += a[k] * b[k];
    });
    Object.keys(b).forEach(function (k) { nb += b[k] * b[k]; });
    if (!na || !nb) return 0;
    return dot / Math.sqrt(na * nb);
  }

  /* How much two members' libraries agree. Rating agreement on the games both
     have played, pulled toward a genre-affinity prior by how little overlap
     there is — so a single shared five-star game cannot claim a 100% match. */
  function tasteMatch(userA, userB) {
    var a = ratingsOf(userA);
    var b = ratingsOf(userB);
    var shared = Object.keys(a).filter(function (g) { return b[g] !== undefined; });

    var agreement = null;
    var pairs = shared.map(function (g) {
      return { game: g, mine: a[g], theirs: b[g], gap: Math.abs(a[g] - b[g]) };
    });
    if (pairs.length) {
      var total = pairs.reduce(function (n, p) {
        return n + (1 - Math.min(p.gap, TASTE_GAP) / TASTE_GAP);
      }, 0);
      agreement = total / pairs.length;
    }

    var genre = cosine(genreVector(userA), genreVector(userB));
    var n = pairs.length;
    var raw = n ?
      (agreement * n + genre * TASTE_PRIOR) / (n + TASTE_PRIOR) : genre;

    /* Confidence in the comparison as a whole, set by the smaller of the two
       libraries. Reaches 1 at TASTE_LIBRARY ratings, so established members are
       untouched and thin ones cannot claim a confident score in either
       direction. */
    var smaller = Math.min(Object.keys(a).length, Object.keys(b).length);
    var confidence = Math.min(1, smaller / TASTE_LIBRARY);
    var score = TASTE_NEUTRAL + (raw - TASTE_NEUTRAL) * confidence;

    /* Games they both rated highly and rated alike — the reason for the score. */
    var loved = pairs.filter(function (p) {
      return p.gap <= 0.5 && p.mine >= 4 && p.theirs >= 4;
    }).sort(function (x, y) {
      return (y.mine + y.theirs) - (x.mine + x.theirs) || x.gap - y.gap;
    });

    var clash = pairs.filter(function (p) { return p.gap >= 1.5; })
      .sort(function (x, y) { return y.gap - x.gap; });

    return {
      score: score,
      percent: Math.round(score * 100),
      shared: n,
      agreement: agreement,
      genre: genre,
      confidence: confidence,
      basis: n >= TASTE_MIN ? 'ratings' : 'genres',
      loved: loved,
      clash: clash
    };
  }

  /* Members ranked by how closely their taste matches this one. */
  function similarMembers(username, opts) {
    opts = opts || {};
    var circle = Object.create(null);
    if (opts.excludeFollowing) {
      followingOf(username).forEach(function (u) { circle[u] = 1; });
    }
    return allMembers()
      .filter(function (m) {
        return m.username !== username && !circle[m.username];
      })
      .map(function (m) {
        var match = tasteMatch(username, m.username);
        match.member = m;
        return match;
      })
      .sort(function (x, y) {
        return y.score - x.score || y.shared - x.shared;
      })
      .slice(0, opts.limit || 99);
  }

  /* -------------------------------------------------------------- comments */

  var COMMENT_MAX = 600;
  var commentCache = null;

  /* Seeded threads plus anything written in this browser, minus whatever has
     been deleted. Built once and reused, the same as allLogs — a game page with
     twenty reviews would otherwise walk both lists twenty times. */
  function allComments() {
    if (commentCache) return commentCache;
    var hidden = load().hiddenComments;
    var mine = load().comments.filter(function (c) { return !c.deleted; });
    commentCache = PG.data.seedComments
      .filter(function (c) { return !hidden[c.id]; })
      .concat(mine);
    return commentCache;
  }

  function byDate(a, b) {
    return String(a.date).localeCompare(String(b.date)) ||
      String(a.id).localeCompare(String(b.id));
  }

  /* Oldest first: a thread reads top to bottom, unlike the diary feeds. */
  function commentsFor(logId) {
    return allComments().filter(function (c) { return c.log === logId; }).sort(byDate);
  }

  function commentCount(logId) {
    var n = 0;
    allComments().forEach(function (c) { if (c.log === logId) n++; });
    return n;
  }

  function addComment(logId, body) {
    var me = currentUser();
    if (!me) return { error: 'Sign in to join the conversation.' };
    var text = String(body || '').trim();
    if (!text) return { error: 'Write something first.' };
    if (text.length > COMMENT_MAX) {
      return { error: 'Comments are ' + COMMENT_MAX + ' characters or fewer.' };
    }
    if (!PG.moderate.check(text).clean) {
      PG.moderate.recordBlock();
      return { error: PG.moderate.message };
    }
    var comment = {
      id: 'c-' + Date.now().toString(36) + Math.floor(Math.random() * 1e4).toString(36),
      log: logId,
      user: me.username,
      /* Full timestamp, not just the day: several comments posted in one sitting
         still need to sort in the order they were written. */
      date: new Date().toISOString(),
      body: text
    };
    load().comments.push(comment);
    commentCache = null;
    save();
    return { comment: comment };
  }

  function findComment(id) {
    return allComments().filter(function (c) { return c.id === id; })[0] || null;
  }

  /* Your own comment, or any comment on a review you wrote — the same
     moderation rule every comment section has. */
  function canDeleteComment(comment) {
    var me = currentUser();
    if (!me || !comment) return false;
    if (comment.user === me.username) return true;
    var log = allLogs().filter(function (l) { return l.id === comment.log; })[0];
    return !!log && log.user === me.username;
  }

  function removeComment(id) {
    var comment = findComment(id);
    if (!comment) return { error: 'That comment is already gone.' };
    if (!canDeleteComment(comment)) {
      return { error: 'You can only delete your own comments.' };
    }
    var own = load().comments.filter(function (c) { return c.id === id; })[0];
    if (own) own.deleted = true;
    else load().hiddenComments[id] = 1;   /* seeded: remember that it is gone */
    commentCache = null;
    save();
    return { removed: true };
  }

  /* ---------------------------------------------------------- review likes */

  function reviewLikes(log) {
    var mine = load().reviewLikes[log.id] ? 1 : 0;
    return (log.likes || 0) + mine;
  }

  function likedReview(logId) {
    return !!load().reviewLikes[logId];
  }

  function toggleReviewLike(logId) {
    var me = currentUser();
    if (!me) return { error: 'Sign in to like a review.' };
    var likes = load().reviewLikes;
    if (likes[logId]) delete likes[logId];
    else likes[logId] = 1;
    save();
    return { liked: !!likes[logId] };
  }

  /* ----------------------------------------------------------- aggregation */

  /* Community score for a game: the stable baseline distribution plus every
     real diary entry, so member ratings visibly move the needle. */
  function ratingSummary(gameId) {
    var game = PG.data.byId[gameId];
    var counts = game.baseCounts.slice();
    logsFor(gameId).forEach(function (l) {
      if (!l.rating) return;
      var i = Math.round(l.rating * 2) - 1;
      if (i >= 0 && i < counts.length) counts[i] += 1;
    });
    var total = 0, weighted = 0;
    counts.forEach(function (c, i) {
      total += c;
      weighted += c * PG.data.buckets[i];
    });
    return {
      counts: counts,
      total: total,
      average: total ? weighted / total : 0,
      max: Math.max.apply(null, counts)
    };
  }

  function logsByStatus(username, status) {
    return logsBy(username).filter(function (l) {
      return statusOf(l) === status;
    });
  }

  function memberStats(username) {
    var all = logsBy(username);
    var logs = all.filter(function (l) { return l.rating; });
    var year = new Date().getFullYear();
    var sum = 0;
    var thisYear = 0;
    var hours = 0;
    var counts = { playing: 0, finished: 0, abandoned: 0 };
    logs.forEach(function (l) {
      sum += l.rating;
      if (l.date && parseInt(l.date.slice(0, 4), 10) === year) thisYear++;
    });
    all.forEach(function (l) {
      counts[statusOf(l)]++;
      if (l.hours) hours += l.hours;
    });
    return {
      logged: logs.length,
      thisYear: thisYear,
      average: logs.length ? sum / logs.length : 0,
      reviews: all.filter(function (l) { return l.review; }).length,
      liked: all.filter(function (l) { return l.liked; }).length,
      backlog: backlogFor(username).length,
      playing: counts.playing,
      finished: counts.finished,
      abandoned: counts.abandoned,
      hours: hours
    };
  }

  /* Wipes everything this browser has stored — accounts, diary, comments. The
     seeded catalogue and community come back from data.js on the next load. */
  function reset() {
    try { window.localStorage.removeItem(KEY); } catch (e) {}
    state = null;
    logCache = null;
    commentCache = null;
    genreCache = null;
    messageCache = null;
  }

  PG.store = {
    demoPassword: DEMO_PASSWORD,
    allMembers: allMembers,
    findMember: findMember,
    register: register,
    login: login,
    logout: logout,
    currentUser: currentUser,
    updateProfile: updateProfile,
    nameMax: NAME_MAX,
    bioMax: BIO_MAX,
    allLogs: allLogs,
    logsFor: logsFor,
    logsBy: logsBy,
    myLog: myLog,
    saveLog: saveLog,
    quickRate: quickRate,
    quickState: quickState,
    removeLog: removeLog,
    statusOf: statusOf,
    logsByStatus: logsByStatus,
    states: STATES,
    backlogFor: backlogFor,
    inBacklog: inBacklog,
    setBacklog: setBacklog,
    followingOf: followingOf,
    followersOf: followersOf,
    followCounts: followCounts,
    isFollowing: isFollowing,
    toggleFollow: toggleFollow,
    logsFromFollowing: logsFromFollowing,
    canMessage: canMessage,
    messagesWith: messagesWith,
    conversations: conversations,
    unreadWith: unreadWith,
    unreadTotal: unreadTotal,
    markRead: markRead,
    sendMessage: sendMessage,
    messageMax: MESSAGE_MAX,
    tasteMatch: tasteMatch,
    similarMembers: similarMembers,
    tasteMinShared: TASTE_MIN,
    reviewLikes: reviewLikes,
    likedReview: likedReview,
    toggleReviewLike: toggleReviewLike,
    commentsFor: commentsFor,
    commentCount: commentCount,
    addComment: addComment,
    removeComment: removeComment,
    canDeleteComment: canDeleteComment,
    commentMax: COMMENT_MAX,
    reviewMax: REVIEW_MAX,
    ratingSummary: ratingSummary,
    memberStats: memberStats,
    reset: reset
  };
})(window.PG);
