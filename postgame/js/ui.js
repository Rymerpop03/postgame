/* Postgame — presentation helpers
   Small pure functions that turn data into markup, plus the toast and modal
   plumbing. Nothing in here touches the router. */
window.PG = window.PG || {};
(function (PG) {
  'use strict';

  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
    'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  var STOP = { the: 1, a: 1, of: 1, and: 1, to: 1 };

  function esc(value) {
    return String(value === undefined || value === null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function attr(value) { return esc(value).replace(/\s+/g, ' '); }

  /* Numbers that land in a style attribute or an unquoted-ish slot get coerced
     here rather than trusted. esc() is the wrong tool for a CSS context — it
     would not stop a value closing the declaration — so these clamp to a real
     number instead. The store already validates hue on write; this is the
     second half of that guarantee, for the day the data arrives from a server
     instead of from the settings form. */
  function hue(value) {
    var n = Number(value);
    return isFinite(n) ? ((Math.round(n) % 360) + 360) % 360 : 260;
  }

  function num(value, fallback) {
    var n = Number(value);
    return isFinite(n) ? n : (fallback || 0);
  }

  /* ----------------------------------------------------------------- dates */

  function parseDate(iso) {
    if (!iso) return null;
    var p = String(iso).slice(0, 10).split('-');
    if (p.length < 3) return null;
    return new Date(+p[0], +p[1] - 1, +p[2]);
  }

  function fmtDate(iso) {
    var d = parseDate(iso);
    if (!d || isNaN(d)) return '';
    return d.getDate() + ' ' + MONTHS[d.getMonth()] + ' ' + d.getFullYear();
  }

  function fmtRelative(iso) {
    var d = parseDate(iso);
    if (!d || isNaN(d)) return '';
    var days = Math.round((Date.now() - d.getTime()) / 86400000);
    if (days <= 0) return 'today';
    if (days === 1) return 'yesterday';
    if (days < 30) return days + ' days ago';
    if (days < 365) {
      var m = Math.round(days / 30);
      return m + (m === 1 ? ' month ago' : ' months ago');
    }
    return fmtDate(iso);
  }

  function fmtNumber(n) {
    return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  function fmtRating(n) {
    if (!n) return '—';
    return (Math.round(n * 10) / 10).toFixed(1);
  }

  /* ------------------------------------------------------------- fragments */

  function initials(title) {
    var words = title.replace(/[^A-Za-z0-9 ]/g, ' ').split(/\s+/)
      .filter(function (w) { return w && !STOP[w.toLowerCase()]; });
    if (!words.length) return title.slice(0, 2).toUpperCase();
    if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
    return (words[0][0] + words[1][0]).toUpperCase();
  }

  /* Procedural cover art: a deterministic gradient, a pattern and the title
     set in the cover's own container units, so one component works at every
     size from a 30px search result to a 260px game page poster. */
  function cover(game, opts) {
    opts = opts || {};
    var body = opts.mini ? '' :
      '<div class="cover__body">' +
        '<div class="cover__title">' + esc(game.title) + '</div>' +
        '<div class="cover__sub">' + esc(game.developer) + '</div>' +
      '</div>';
    return '<div class="cover" style="--h:' + hue(game.hue) + '" data-pattern="' +
      attr(game.pattern) + '" data-game="' + attr(game.id) +
      '" role="img" aria-label="' + attr(game.title) + ' cover art">' +
      '<div class="cover__pat"></div>' +
      '<div class="cover__glyph" aria-hidden="true">' + esc(initials(game.title)) + '</div>' +
      body +
      '</div>';
  }

  function stars(rating, modifier) {
    var pct = Math.max(0, Math.min(5, rating || 0)) / 5 * 100;
    return '<span class="stars ' + (modifier || '') + '" title="' +
      fmtRating(rating) + ' out of 5">' +
      '<span class="stars__fill" style="width:' + pct.toFixed(2) + '%"></span></span>';
  }

  /* Lettered gradient by default; an uploaded picture layers over it. The data
     URL is checked here as well as on save — this builds a CSS url(), and a
     string that reached storage some other way must not be able to close it. */
  function avatar(member, modifier) {
    var name = member.name || member.username;
    var pic = member.avatar && /^data:image\/[a-z]+;base64,[A-Za-z0-9+/=]+$/
      .test(member.avatar) ? member.avatar : '';
    return '<span class="avatar ' + (modifier || '') + (pic ? ' avatar--img' : '') +
      '" style="--h:' + hue(member.hue) +
      (pic ? ';background-image:url(&quot;' + pic + '&quot;)' : '') +
      '" aria-hidden="true">' + esc(name.slice(0, 1)) + '</span>';
  }

  /* Sprite ids carry an icon- prefix so they live in their own namespace. They
     used to be bare, which collided with the element id="search" on the header
     search box: getElementById returned the <symbol>, since the sprite comes
     first in document order, and the dropdown's is-open class was applied to
     it instead of the container. Keep the prefix. */
  function icon(id, cls) {
    return '<svg class="' + (cls || 'btn__ico') + '" aria-hidden="true"><use href="#icon-' +
      id + '"></use></svg>';
  }

  /* --------------------------------------------------------------- widgets */

  var STATE_LABEL = { playing: 'Playing', finished: 'Finished', abandoned: 'Abandoned' };
  var STATE_GLYPH = { playing: '▶', abandoned: '✕' };

  /* A rating control that saves the moment it is clicked. The log modal keeps
     its own copy of this widget, because there the score is a draft that is
     not written until the form is submitted. */
  function rateStrip(gameId, value, modifier, tabbable) {
    var pct = (Math.max(0, Math.min(5, value || 0)) / 5 * 100).toFixed(2);
    /* Ten buttons per cover would be 480 tab stops on a full grid page, so the
       strips on tiles are pointer-only. The game page and the log form carry
       the same control with the keyboard path intact. */
    var skip = tabbable === false ? ' tabindex="-1" aria-hidden="true"' : '';
    var hits = '';
    for (var i = 1; i <= 10; i++) {
      var v = i / 2;
      hits += '<button type="button"' + skip + ' data-act="quick-rate" data-game="' +
        attr(gameId) + '" data-v="' + v + '" aria-label="Rate ' + v +
        ' out of 5" title="' + v + (v === 1 ? ' star' : ' stars') + '"></button>';
    }
    return '<span class="rate ' + (modifier || '') + '" data-rate-for="' + attr(gameId) +
      '" data-rate-value="' + num(value) + '">' +
      '<span class="rate__track"><span class="rate__on" style="width:' + pct + '%"></span></span>' +
      '<span class="rate__hit">' + hits + '</span></span>';
  }

  function statusChip(log, showFinished) {
    var state = PG.store.statusOf(log);
    if (state === 'finished' && !showFinished) return '';
    return '<span class="statechip statechip--' + state + '">' +
      STATE_LABEL[state] + '</span>';
  }

  function fmtHours(h) {
    return (Math.round(h * 10) / 10) + 'h';
  }

  /* Hours played and the platform it was played on, when the entry has them. */
  function spec(log) {
    var bits = [];
    if (log.hours) bits.push(esc(fmtHours(log.hours)));
    if (log.platform) bits.push('on ' + esc(log.platform));
    return bits.length ? '<div class="review__spec">' + bits.join(' · ') + '</div>' : '';
  }

  function tile(game, opts) {
    opts = opts || {};
    var log = opts.log;
    var strip = '';
    if (opts.rate) {
      /* Signed in: the strip is live, and starts from whatever is logged. */
      strip = '<div class="tile__own' + (log && log.rating ? '' : ' tile__own--blank') + '">' +
        rateStrip(game.id, log ? log.rating : 0, 'rate--mini', false) +
        (log && log.liked ? '<span class="heart" title="Liked">♥</span>' : '') + '</div>';
    } else if (log) {
      strip = '<div class="tile__own">' + stars(log.rating, 'stars--sm') +
        (log.liked ? '<span class="heart" title="Liked">♥</span>' : '') + '</div>';
    }

    var state = log ? PG.store.statusOf(log) : null;
    var badge = '';
    if (STATE_GLYPH[state]) {
      badge = '<span class="tile__flag tile__flag--' + state + '" title="' +
        STATE_LABEL[state] + '">' + STATE_GLYPH[state] + '</span>';
    } else if (opts.flag) {
      badge = '<span class="tile__flag" title="' + attr(opts.flag) + '">+</span>';
    }

    var sub = opts.sub === false ? '' :
      '<div class="tile__sub">' + esc(game.year) +
      (opts.rating ? '<span>·</span>' + stars(opts.rating, 'stars--sm') : '') +
      '</div>';

    /* The cover link is an overlay rather than a wrapper, so the rating
       buttons can sit on top of it without nesting controls inside an <a>. */
    return '<div class="tile">' +
      '<div class="tile__cover">' + cover(game) +
        '<a class="tile__hit" href="#/game/' + attr(game.id) + '" aria-label="' +
          attr(game.title) + ' (' + attr(game.year) + ')"></a>' +
        strip + badge +
      '</div>' +
      '<a class="tile__title" href="#/game/' + attr(game.id) + '">' +
        esc(game.title) + '</a>' + sub +
      '</div>';
  }

  function grid(games, mapOpts, cls) {
    if (!games.length) return empty('Nothing here yet');
    return '<div class="grid ' + (cls || '') + '">' + games.map(function (g) {
      return tile(g, mapOpts ? mapOpts(g) : null);
    }).join('') + '</div>';
  }

  function empty(title, body) {
    return '<div class="empty"><div class="empty__t">' + esc(title) + '</div>' +
      (body ? '<div>' + esc(body) + '</div>' : '') + '</div>';
  }

  function paragraphs(text) {
    return String(text).split(/\n{2,}/).map(function (p) {
      return '<p>' + esc(p).replace(/\n/g, '<br>') + '</p>';
    }).join('');
  }

  function review(log, opts) {
    opts = opts || {};
    var member = PG.store.findMember(log.user) || { username: log.user, name: log.user, hue: 260 };
    var game = PG.catalogue.resolve(log.game);
    var likes = PG.store.reviewLikes(log);
    var mine = opts.mine;
    var who = function (cls) {
      return '<a class="' + cls + '" href="#/member/' + attr(member.username) + '">' +
        esc(member.name) + '</a>';
    };
    /* Where the game is named, it leads the headline row beside the stars and
       the member becomes the byline above — the rating is about the game, so
       that is the pairing that reads. On a game's own page there is no title to
       repeat, so the member takes the headline slot instead. */
    var title = opts.showGame && game ?
      '<a class="review__game" href="#/game/' + attr(game.id) + '">' +
        esc(game.title) + ' <span class="review__year">' +
        esc(game.year) + '</span></a>' : '';
    return '<article class="review">' +
      '<a href="#/member/' + attr(member.username) + '" aria-label="' +
        attr(member.name) + '">' + avatar(member, 'avatar--sm') + '</a>' +
      '<div>' +
        (title ? who('review__byline') : '') +
        '<div class="review__head">' +
          (title || who('review__who')) +
          stars(log.rating, 'stars--sm') +
          (log.liked ? '<span class="heart" title="Liked">♥</span>' : '') +
          statusChip(log, opts.showFinished) +
          (log.replay ? '<span class="review__badge">Replay</span>' : '') +
          (mine ? '<span class="review__badge">Your log</span>' : '') +
          '<span class="review__when">' + esc(fmtDate(log.date)) + '</span>' +
        '</div>' +
        spec(log) +
        (log.review ? '<div class="review__body">' +
          paragraphs(PG.moderate.mask(log.review)) + '</div>' : '') +
        '<div class="review__foot">' +
          '<button class="likebtn ' + (PG.store.likedReview(log.id) ? 'is-on' : '') +
            '" data-act="like-review" data-id="' + attr(log.id) + '">' +
            icon('heart', '') + '<span>' + fmtNumber(likes) + '</span></button>' +
          commentToggle(log, opts.openThread) +
          (mine ? '<button class="linkbtn" data-act="log" data-game="' +
            attr(log.game) + '">Edit</button>' : '') +
        '</div>' +
        (opts.openThread ? thread(log) : '') +
      '</div>' +
      '</article>';
  }

  /* ------------------------------------------------------------- following */

  /* Nothing on your own profile — the rest of the time the button shows, even
     signed out, so the affordance is discoverable; the action prompts to sign
     in. Same reasoning as the want-to-play button. */
  function followButton(username, modifier) {
    var me = PG.store.currentUser();
    if (me && me.username === username) return '';
    var on = PG.store.isFollowing(username);
    return '<button class="btn ' + (modifier || '') + (on ? ' btn--soft' : ' btn--primary') +
      '" data-act="follow" data-user="' + attr(username) +
      '" aria-pressed="' + (on ? 'true' : 'false') + '">' +
      (on ? icon('check') + 'Following' : icon('plus') + 'Follow') + '</button>';
  }

  /* ----------------------------------------------------------- taste match */

  /* A bare percentage is hard to read when the realistic range across a small
     community is roughly 45-85 — 55% sounds like a failure when it is in fact
     mid-table. The band is what makes the number interpretable. */
  var TASTE_BANDS = [
    [75, 'Strong match', 'strong'],
    [65, 'Similar taste', 'similar'],
    [55, 'Some overlap', 'some'],
    [0, 'Different tastes', 'weak']
  ];

  function tasteBand(percent) {
    for (var i = 0; i < TASTE_BANDS.length; i++) {
      if (percent >= TASTE_BANDS[i][0]) {
        return { label: TASTE_BANDS[i][1], key: TASTE_BANDS[i][2] };
      }
    }
    return { label: TASTE_BANDS[3][1], key: TASTE_BANDS[3][2] };
  }

  function matchBadge(match) {
    var band = tasteBand(match.percent);
    return '<span class="matchpill matchpill--' + band.key + '" title="' +
      attr(band.label + ' — ' + (match.shared ?
        match.shared + ' games in common' : 'based on genres')) + '">' +
      match.percent + '% match</span>';
  }

  function gameChips(entries, limit) {
    return entries.slice(0, limit).map(function (p) {
      var game = PG.catalogue.resolve(p.game);
      if (!game) return '';
      return '<a class="chip" href="#/game/' + attr(game.id) + '">' +
        esc(game.title) + '</a>';
    }).join('');
  }

  /* The comparison between the signed-in member and whoever's profile this is.
     Shows its working: the games behind the score, and the ones it is arguing
     with. */
  function tastePanel(match, them) {
    var band = tasteBand(match.percent);
    var enough = match.basis === 'ratings';

    var lead = enough ?
      esc(match.shared + ' game' + (match.shared === 1 ? '' : 's') +
        ' you have both rated') :
      'Too few games in common to compare ratings — this is based on the ' +
        'genres you each reach for';

    var body = '';
    if (match.loved.length) {
      body += '<div class="taste__group">' +
        '<div class="taste__k">You both rated highly</div>' +
        '<div class="chips">' + gameChips(match.loved, 4) + '</div></div>';
    }
    if (match.clash.length) {
      body += '<div class="taste__group">' +
        '<div class="taste__k">You disagree on</div>' +
        '<div class="taste__clash">' + match.clash.slice(0, 2).map(function (p) {
          var game = PG.catalogue.resolve(p.game);
          if (!game) return '';
          return '<div class="taste__row">' +
            '<a class="taste__game" href="#/game/' + attr(game.id) + '">' +
              esc(game.title) + '</a>' +
            '<span class="taste__vs">you <b>' + fmtRating(p.mine) + '</b>' +
              ' · them <b>' + fmtRating(p.theirs) + '</b></span>' +
            '</div>';
        }).join('') + '</div></div>';
    }
    if (!body) {
      body = '<div class="taste__group"><div class="taste__k">Nothing in common yet</div>' +
        '<p class="taste__none">Rate a few of the same games and this fills in.</p></div>';
    }

    return '<section class="taste">' +
      '<div class="taste__score taste__score--' + band.key + '">' +
        '<div class="taste__pct">' + match.percent + '<span>%</span></div>' +
        '<div class="taste__band">' + esc(band.label) + '</div>' +
      '</div>' +
      '<div class="taste__detail">' +
        '<p class="taste__lead">Taste match with ' + esc(them.name) +
          '<span class="taste__basis">' + lead + '</span></p>' +
        body +
      '</div>' +
      '</section>';
  }

  /* One member card, used by the members list and by the follower/following
     pages. The link is an overlay rather than a wrapper so the follow button
     can sit inside the card without nesting a control in an anchor. */
  function memberCard(member, opts) {
    opts = opts || {};
    var stats = PG.store.memberStats(member.username);
    var me = PG.store.currentUser();
    var isMe = !!me && me.username === member.username;
    return '<div class="mcard">' +
      '<a class="mcard__hit" href="#/member/' + attr(member.username) +
        '" aria-label="' + attr(member.name) + ' (@' + attr(member.username) + ')"></a>' +
      avatar(member) +
      '<div class="mcard__meta">' +
        '<div class="mcard__name">' + esc(member.name) +
          (isMe ? ' <span class="review__badge">You</span>' : '') + '</div>' +
        '<div class="mcard__handle">@' + esc(member.username) +
          (opts.followsYou ? ' <span class="mcard__tag">Follows you</span>' : '') +
        '</div>' +
        (member.bio ? '<div class="mcard__bio">' + esc(member.bio) + '</div>' : '') +
        '<div class="mcard__stats">' +
          (opts.match ? matchBadge(opts.match) + '<span class="mcard__dot">·</span>' : '') +
          stats.logged + ' logged · ' + stats.reviews + ' reviews</div>' +
      '</div>' +
      (opts.follow === false ? '' :
        '<div class="mcard__do">' + followButton(member.username, 'btn--sm') + '</div>') +
      '</div>';
  }

  function memberList(members, opts) {
    return '<div class="memberlist">' + members.map(function (m) {
      return memberCard(m, opts && opts.each ? opts.each(m) : opts);
    }).join('') + '</div>';
  }

  /* -------------------------------------------------------------- messages */

  function fmtTime(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return '';
    var h = d.getHours();
    var m = d.getMinutes();
    return (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m;
  }

  /* Day heading between runs of messages, so a thread spanning months does not
     read as one continuous conversation. */
  function dayLabel(iso) {
    var rel = fmtRelative(iso);
    return rel === 'today' || rel === 'yesterday' ?
      rel.charAt(0).toUpperCase() + rel.slice(1) : fmtDate(iso);
  }

  function messageList(thread, meName) {
    var out = '';
    var lastDay = '';
    thread.forEach(function (m) {
      var day = String(m.sent).slice(0, 10);
      if (day !== lastDay) {
        out += '<div class="msg__day">' + esc(dayLabel(m.sent)) + '</div>';
        lastDay = day;
      }
      var mine = m.from === meName;
      out += '<div class="msg' + (mine ? ' msg--mine' : '') + '">' +
        '<div class="msg__bubble">' + paragraphs(PG.moderate.mask(m.body)) + '</div>' +
        '<div class="msg__meta">' + esc(fmtTime(m.sent)) + '</div>' +
        '</div>';
    });
    return out;
  }

  function conversationRow(c) {
    var body = c.last ? PG.moderate.mask(c.last.body) : '';
    var preview = c.last ? (c.last.from === (PG.store.currentUser() || {}).username ?
      'You: ' + body : body) : 'No messages yet';
    return '<a class="conv' + (c.unread ? ' is-unread' : '') +
      '" href="#/messages/' + attr(c.member.username) + '">' +
      avatar(c.member) +
      '<div class="conv__meta">' +
        '<div class="conv__top">' +
          '<span class="conv__name">' + esc(c.member.name) + '</span>' +
          (c.last ? '<span class="conv__when">' +
            esc(fmtRelative(c.last.sent)) + '</span>' : '') +
        '</div>' +
        '<div class="conv__preview">' + esc(preview) + '</div>' +
      '</div>' +
      (c.unread ? '<span class="conv__badge">' + fmtNumber(c.unread) + '</span>' : '') +
      '</a>';
  }

  /* ---------------------------------------------------------- conversation */

  /* Turns @handle into a link, but only when the handle is a real member —
     otherwise an email address or a stray @ in prose becomes a dead link.
     Runs on already-escaped text, so the markup it inserts is the only markup. */
  function mentions(escaped) {
    return escaped.replace(/@([a-zA-Z0-9_]{3,20})/g, function (whole, handle) {
      var member = PG.store.findMember(handle);
      if (!member) return whole;
      return '<a class="mention" href="#/member/' + attr(member.username) + '">@' +
        esc(member.username) + '</a>';
    });
  }

  function commentBody(text) {
    return String(PG.moderate.mask(text)).split(/\n{2,}/).map(function (p) {
      return '<p>' + mentions(esc(p).replace(/\n/g, '<br>')) + '</p>';
    }).join('');
  }

  function comment(c, log) {
    var member = PG.store.findMember(c.user) ||
      { username: c.user, name: c.user, hue: 260 };
    var isAuthor = log && c.user === log.user;
    return '<article class="comment" id="comment-' + attr(c.id) + '">' +
      '<a href="#/member/' + attr(member.username) + '" tabindex="-1" aria-hidden="true">' +
        avatar(member, 'avatar--sm') + '</a>' +
      '<div class="comment__main">' +
        '<div class="comment__head">' +
          '<a class="comment__who" href="#/member/' + attr(member.username) + '">' +
            esc(member.name) + '</a>' +
          (isAuthor ? '<span class="comment__badge">Author</span>' : '') +
          '<span class="comment__when" title="' + attr(fmtDate(c.date)) + '">' +
            esc(fmtRelative(c.date)) + '</span>' +
        '</div>' +
        '<div class="comment__body">' + commentBody(c.body) + '</div>' +
        '<div class="comment__foot">' +
          '<button class="linkbtn" data-act="reply-comment" data-log="' +
            attr(c.log) + '" data-user="' + attr(member.username) + '">Reply</button>' +
          (PG.store.canDeleteComment(c) ?
            '<button class="linkbtn linkbtn--quiet" data-act="delete-comment" data-id="' +
              attr(c.id) + '">Delete</button>' : '') +
        '</div>' +
      '</div>' +
      '</article>';
  }

  /* The conversation under one review: the comments, then a box to add to them.
     Collapsed by default — the activity feed would be unreadable otherwise. */
  function thread(log) {
    var list = PG.store.commentsFor(log.id);
    var me = PG.store.currentUser();
    var compose = me ?
      '<form class="cform" data-log="' + attr(log.id) + '">' +
        '<textarea class="textarea textarea--sm" name="body" rows="2" maxlength="' +
          PG.store.commentMax + '" placeholder="Add a comment…" aria-label="Add a comment"></textarea>' +
        '<div class="cform__foot">' +
          '<span class="charcount">0 / ' + PG.store.commentMax + '</span>' +
          '<button class="btn btn--primary btn--sm" type="submit">Post</button>' +
        '</div>' +
      '</form>' :
      '<div class="cform__out">' +
        '<a href="#/signin">Sign in</a> to join the conversation.' +
      '</div>';

    return '<div class="thread" id="thread-' + attr(log.id) + '">' +
      (list.length ?
        '<div class="thread__list">' + list.map(function (c) {
          return comment(c, log);
        }).join('') + '</div>' :
        '<p class="thread__none">No comments yet. Start the conversation.</p>') +
      compose +
      '</div>';
  }

  function commentToggle(log, open) {
    var n = PG.store.commentCount(log.id);
    /* aria-controls only while the thread is actually in the document. The
       thread is rendered on demand, so advertising it when collapsed points
       assistive tech at an id that is not there. */
    var controls = open ? ' aria-controls="thread-' + attr(log.id) + '"' : '';
    return '<button class="linkbtn' + (open ? ' is-on' : '') +
      '" data-act="toggle-thread" data-id="' + attr(log.id) +
      '" aria-expanded="' + (open ? 'true' : 'false') + '"' + controls + '>' +
      (n ? fmtNumber(n) + (n === 1 ? ' comment' : ' comments') : 'Comment') +
      '</button>';
  }

  function feedRow(log) {
    var member = PG.store.findMember(log.user) || { username: log.user, name: log.user, hue: 260 };
    var game = PG.catalogue.resolve(log.game);
    if (!game) return '';
    return '<div class="feed__row">' +
      '<a class="feed__cover" href="#/game/' + attr(game.id) + '" aria-hidden="true" tabindex="-1">' +
        cover(game, { mini: true }) + '</a>' +
      '<div class="feed__line">' +
        '<a href="#/member/' + attr(member.username) + '"><b>' + esc(member.name) +
          '</b></a> logged <a href="#/game/' + attr(game.id) + '"><b>' +
          esc(game.title) + '</b></a>' +
        (log.review ? '<div class="feed__quote">' +
          esc(PG.moderate.mask(log.review)) + '</div>' : '') +
      '</div>' +
      '<div class="feed__right">' +
        stars(log.rating, 'stars--sm') +
        '<span class="feed__date">' + statusChip(log) +
          esc(fmtRelative(log.date)) + '</span>' +
      '</div>' +
      '</div>';
  }

  function histogram(summary) {
    var max = summary.max || 1;
    var cols = summary.counts.map(function (c, i) {
      var v = PG.data.buckets[i];
      var h = Math.max(2, Math.round((c / max) * 100));
      return '<div class="hist__col" data-tip="' + fmtNumber(c) + ' × ' + v +
        (v === 1 ? ' star' : ' stars') + '">' +
        '<div class="hist__bar" style="height:' + h + '%"></div></div>';
    }).join('');
    return '<div class="hist">' + cols + '</div>' +
      '<div class="hist__legend"><span>★</span><span>★★★★★</span></div>';
  }

  /* Finished, playing and abandoned are not peers of "games logged" — they are
     its parts. Shown as three more identical boxes they read as unrelated
     metrics, so they get a proportion bar instead, which says at a glance
     whether someone finishes what they start. */
  function playSplit(counts) {
    var parts = [
      ['finished', counts.finished, 'finished'],
      ['playing', counts.playing, 'playing'],
      ['abandoned', counts.abandoned, 'abandoned']
    ].filter(function (p) { return p[1] > 0; });
    var total = parts.reduce(function (n, p) { return n + p[1]; }, 0);
    if (!total) return '';

    var caption = parts.map(function (p) {
      return p[1] + ' ' + p[2];
    }).join(', ');

    return '<div class="stats__split">' +
      '<div class="split__bar" role="img" aria-label="' + attr(caption) + '">' +
        parts.map(function (p) {
          return '<span class="split__seg split__seg--' + p[0] +
            '" style="width:' + (p[1] / total * 100).toFixed(2) + '%"></span>';
        }).join('') +
      '</div>' +
      '<div class="split__legend" aria-hidden="true">' + parts.map(function (p) {
        return '<span class="split__key split__key--' + p[0] + '"><b>' +
          fmtNumber(p[1]) + '</b> ' + p[2] + '</span>';
      }).join('') + '</div>' +
      '</div>';
  }

  /* items: [value, label, optional unit]. The unit rides along with the number
     rather than going in the label, so "536 h" stays one readable figure and
     the label underneath stays a single short word that never wraps. */
  function statBlock(items, counts) {
    return '<div class="stats">' +
      '<div class="stats__row">' + items.map(function (it) {
        return '<div class="stat">' +
          '<div class="stat__v">' + esc(it[0]) +
            (it[2] ? '<span class="stat__u">' + esc(it[2]) + '</span>' : '') +
          '</div>' +
          '<div class="stat__k">' + esc(it[1]) + '</div>' +
          '</div>';
      }).join('') + '</div>' +
      (counts ? playSplit(counts) : '') +
      '</div>';
  }

  /* ---------------------------------------------------------------- toasts */

  function toast(message, bad) {
    var host = document.getElementById('toasts');
    if (!host) return;
    var el = document.createElement('div');
    el.className = 'toast' + (bad ? ' toast--bad' : '');
    el.textContent = message;
    host.appendChild(el);
    window.setTimeout(function () {
      el.classList.add('is-out');
      window.setTimeout(function () { el.remove(); }, 320);
    }, 2400);
  }

  /* ----------------------------------------------------------------- modal */

  var modalEl = null;
  var lastFocus = null;
  var dialogSeq = 0;

  var FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]),' +
    'select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';

  /* The page behind an open dialog is taken out of play entirely, for the
     keyboard and for assistive tech. #modal is a sibling of these three, so
     nothing that holds focus is ever inside an inert subtree. */
  var BACKDROP = ['header.top', 'main', 'footer.foot'];

  function setBackdropInert(on) {
    BACKDROP.forEach(function (sel) {
      var el = document.querySelector(sel);
      if (!el) return;
      if (on) {
        el.setAttribute('inert', '');
        el.setAttribute('aria-hidden', 'true');
      } else {
        el.removeAttribute('inert');
        el.removeAttribute('aria-hidden');
      }
    });
  }

  function tabbable(panel) {
    return Array.prototype.filter.call(panel.querySelectorAll(FOCUSABLE), function (el) {
      return el.offsetWidth || el.offsetHeight || el.getClientRects().length;
    });
  }

  /* Wrap Tab at both ends so focus cannot walk out into the page underneath.
     Attached once, and a no-op unless a dialog is up. */
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Tab' || !modalEl || !modalIsOpen()) return;
    var panel = modalEl.querySelector('.modal__panel');
    if (!panel) return;
    var items = tabbable(panel);
    if (!items.length) return;
    var first = items[0];
    var last = items[items.length - 1];
    if (!panel.contains(document.activeElement)) {
      e.preventDefault();
      first.focus();
    } else if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  });

  function openModal(html) {
    modalEl = modalEl || document.getElementById('modal');
    lastFocus = document.activeElement;
    modalEl.innerHTML = '<div class="modal__panel" role="dialog" aria-modal="true">' +
      html + '</div>';
    modalEl.classList.add('is-open');
    document.body.style.overflow = 'hidden';
    setBackdropInert(true);

    /* Name the dialog from its own heading. Without this a screen reader falls
       back to reading the whole panel as the dialog's name. */
    var panel = modalEl.querySelector('.modal__panel');
    var heading = panel.querySelector('h1, h2, h3, .modal__title');
    if (heading) {
      if (!heading.id) heading.id = 'dialog-title-' + (++dialogSeq);
      panel.setAttribute('aria-labelledby', heading.id);
    } else {
      panel.setAttribute('aria-label', 'Dialog');
    }

    /* Three separate lookups, not one comma-separated selector: querySelector
       returns the first match in *document order*, so a single list would hand
       focus to the close button in the header before ever reaching the field
       that asked for it. */
    var focusable = modalEl.querySelector('[data-autofocus]') ||
      modalEl.querySelector('.btn--primary') ||
      modalEl.querySelector('button');
    if (focusable) focusable.focus();
  }

  function closeModal() {
    modalEl = modalEl || document.getElementById('modal');
    modalEl.classList.remove('is-open');
    modalEl.innerHTML = '';
    document.body.style.overflow = '';
    setBackdropInert(false);
    if (lastFocus && lastFocus.focus) lastFocus.focus();
    lastFocus = null;
  }

  function modalIsOpen() {
    modalEl = modalEl || document.getElementById('modal');
    return modalEl.classList.contains('is-open');
  }

  /* Deliberately narrow. Everything below is used by app.js; helpers that
     only serve this module's own components (tile, comment, thread,
     memberCard, the formatters) stay private, so the surface another
     module can start depending on is a choice rather than an accident. */
  PG.ui = {
    esc: esc,
    attr: attr,
    hue: hue,
    fmtNumber: fmtNumber,
    fmtRating: fmtRating,
    cover: cover,
    stars: stars,
    rateStrip: rateStrip,
    stateLabel: STATE_LABEL,
    avatar: avatar,
    icon: icon,
    grid: grid,
    empty: empty,
    review: review,
    followButton: followButton,
    memberList: memberList,
    tastePanel: tastePanel,
    messageList: messageList,
    conversationRow: conversationRow,
    feedRow: feedRow,
    histogram: histogram,
    statBlock: statBlock,
    toast: toast,
    openModal: openModal,
    closeModal: closeModal,
    modalIsOpen: modalIsOpen
  };
})(window.PG);
