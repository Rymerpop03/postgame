/* Postgame — content filter
   Blocks slurs and hate speech from member-written text: reviews, comments,
   messages, display names and bios.

   SCOPE. This targets slurs and attacks aimed at people for what they are —
   race, ethnicity, religion, sexual orientation, gender identity, disability.
   Ordinary profanity is deliberately allowed through: "this boss is fucking
   unfair" is a normal thing to write about a video game and filtering it would
   only make the site annoying. The line is who the language is aimed at, not
   how rude it is.

   THE LIST IS HASHED, not stored as text. A file containing a few hundred
   slurs in plain sight is unpleasant to open, easy to leak into a search index,
   and the sort of thing that ends up quoted out of context. Only opaque keys
   are kept here. To add a term, run PG.moderate.hash('term') in the console and
   paste the result into BLOCKED — you never have to type it into the source.
   The trade-off is that the list cannot be read back; PG.moderate.test('word')
   answers whether a given word is caught.

   TWO LAYERS. Text is refused at the point of writing, so it never enters
   storage, and masked again at the point of display. The second pass is what
   makes the guarantee hold for anything already saved, edited in through
   devtools, or restored from an older backup. */
window.PG = window.PG || {};
(function (PG) {
  'use strict';

  /* Characters people substitute to slip past a filter. */
  var LEET = {
    '0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '7': 't',
    '8': 'b', '9': 'g', '$': 's', '@': 'a', '!': 'i', '|': 'i'
  };

  /* Cyrillic and Greek letters that render identically to Latin ones. Pasting a
     Cyrillic "а" into the middle of a word is the oldest trick there is, and
     without this the stripper simply deletes it and the word stops matching. */
  var HOMOGLYPH = {
    'а': 'a', 'в': 'b', 'с': 'c', 'ԁ': 'd', 'е': 'e', 'ѕ': 's', 'һ': 'h',
    'і': 'i', 'ј': 'j', 'к': 'k', 'м': 'm', 'н': 'h', 'о': 'o', 'р': 'p',
    'т': 't', 'у': 'y', 'х': 'x', 'ν': 'v', 'α': 'a', 'ε': 'e', 'ι': 'i',
    'κ': 'k', 'ο': 'o', 'ρ': 'p', 'τ': 't', 'υ': 'u', 'χ': 'x', 'ѵ': 'v'
  };

  /* Everything except run-collapsing: case, accents, look-alikes, leet, and
     any character that is not a letter — so "n.i.g.g.e.r" and "n_i_g_g_e_r"
     both fold to the same run of letters. */
  function fold(text) {
    var s = String(text == null ? '' : text).toLowerCase();
    s = s.normalize ? s.normalize('NFKD') : s;
    s = s.replace(/[̀-ͯ]/g, '');
    s = s.replace(/[^ -~]/g, function (c) { return HOMOGLYPH[c] || c; });
    s = s.replace(/[01345789$@!|]/g, function (c) { return LEET[c] || c; });
    return s.replace(/[^a-z]/g, '');
  }

  /* The form the word list is keyed on. */
  function normalise(text) {
    return fold(text).replace(/(.)\1{2,}/g, '$1$1');
  }

  /* Two readings, and the difference between them is the whole trick.

     A run of three or more identical letters is padding — nobody types
     "reeeetard" by accident — but the real spelling underneath might have one
     letter there or two, so a padded run is tried both ways. A run of exactly
     two is left alone in both readings, because that one is probably genuine:
     collapsing it as well turns "coon" into "con" and starts censoring the
     ordinary word con. */
  function variants(word) {
    var base = fold(word);
    if (!base) return [];
    var two = base.replace(/(.)\1{2,}/g, '$1$1');
    var one = base.replace(/(.)\1{2,}/g, '$1');
    return one === two ? [two] : [two, one];
  }

  /* FNV-1a and djb2 side by side. One 32-bit hash over a list this size has a
     small but real chance of colliding with an ordinary word, and a collision
     here means censoring something innocent with no way to see why. Two
     independent hashes make that vanishingly unlikely. */
  function hash(term) {
    var s = normalise(term);
    if (!s) return '';
    var h1 = 0x811c9dc5;
    var h2 = 0x1505;
    for (var i = 0; i < s.length; i++) {
      var c = s.charCodeAt(i);
      h1 = Math.imul(h1 ^ c, 0x01000193) >>> 0;
      h2 = (((h2 << 5) + h2) + c) >>> 0;
    }
    return h1.toString(16) + h2.toString(16);
  }

  /* Slurs and hate phrases, as keys. Multi-word entries are stored as the
     joined form, because normalise() drops the spaces — "white power" and
     "whitepower" reduce to the same key, and the scanner checks runs of two and
     three words as well as single ones. */
  var BLOCKED = [
    '1285c2c95f033445', '13b9aeb4d3ab74c4', '1414e96a7c9e0974', '1544eac87c9e5730', '17760eaa7c96a886', '1d016fdd7c9e0883',
    '1e65ce2f10570f97', '206035117c97d1f9', '262461cd7c98db33', '29f2bf9c2c1e539', '2be15dc470d151cc', '2ef6335bc140b2f5',
    '32d2fa6fc5fc015', '35f210364e1f29ec', '36c9c3b0c0156406', '3e63c0032a8c068', '4162a79b7c97d199', '41ecf2d2ffeaf20',
    '45bd3c79b88c38b', '4b2884ef29e95eef', '4b73da8cf87455a', '4ce3bf7f510c1b63', '4e9a7720da08561a', '50ce07587c979c24',
    '56494292a670498', '5e10c021e7ebcc31', '5e70e3f3f83e84d5', '62bfa028105ff796', '635b1b38256ffa30', '65339e74123d28a',
    '6c6af64f23047caf', '6d6111e13b57614', '70d1d9e88d6f6854', '717b373ffa93129', '71faa974f47b680', '737d45c4922284a4',
    '76e2faca27ce28c4', '775b5390f924dd2', '7be1c03dbb3fd829', '7eea8f1ef825b24', '7f8fc16d3e1a29f5', '8249237254bbb73',
    '824e0807f6632565', '849299566eb3fb9', '853febae7c9c24ca', '861c6d4220d9d2fe', '87ecf473465edf17', '883eef411ebb6a81',
    '948002a119300f07', '955b3ec5fce2bdbd', '95c1f586f603475a', '964c13635060e903', '970e8106a487025c', '978470963f31f05a',
    '9950ab5d6e3d3e25', '9a874f20cfadda2a', 'a0a04ce71020be7d', 'a4705559101cc581', 'b1f8089db8871b3', 'b5eaf25b105f3867',
    'bb7973ea101cc4fe', 'bb7b0c24bc395924', 'bce248a6f6bb936', 'beeb989cf628b8f6', 'c45f0c37783284d', 'c517291c801cc2f4',
    'c86be502f85a088', 'c9b8e72350c7a28d', 'c9f777d5f6bb833', 'cbedab6cb8882c0', 'd4a3b882993a75d0', 'd7200eed1bb35811',
    'd9eb56dd1000df2b', 'dbaaaa0efcaa67c', 'dc08ae097c9988e9', 'e1fab5d910252107', 'e5ef83fb2ec55a33', 'e7587f0cdfc92ba0',
    'e8e1bb43f3d3ee7', 'ed4d023a15f6908', 'ef08753cf3d34f72', 'f0f80ca44cf56bc', 'f23e2de27c953ff4', 'f3d231f7c7dad747',
    'f6a4b84ef394eb2', 'f729446ae6d36a42', 'f96030457c977175', 'fa015931a56137d', 'fd84968f34f5ae31'
  ];

  var index = Object.create(null);
  BLOCKED.forEach(function (k) { index[k] = 1; });

  /* Innocent phrases that happen to contain a blocked word. "A chink in the
     armour of every boss" is ordinary writing about a video game, and censoring
     it would be both wrong and faintly ridiculous — but the word on its own is
     a serious slur and is worth keeping on the list. When a match sits at the
     start of one of these, it is left alone.

     This is the pressure valve for the whole design: whole-word matching
     already means "Scunthorpe" and "raccoon" are safe, and this covers the
     remaining cases where the word itself is the innocent one. */
  var EXEMPT = [
    '1a04d3772028c08b', '5360b867d15cae23', '6fe9ce674b033393', '8918b242540d2c0', '8a2112cd35b1dde', 'b7751c894007c87f',
    'befb6d696cad44cf', 'f4274f768de8d2fe'
  ];
  var exempt = Object.create(null);
  EXEMPT.forEach(function (k) { exempt[k] = 1; });

  /* Plurals only. This started out stripping -y, -er, -ing and -ed as well,
     which quietly turned "spicy" into a slur, and "Spicer" with it. Every ending
     removed here is an ending that can manufacture a match out of an innocent
     word, and the inflections worth catching are already on the list in full. */
  var SUFFIX = /(?:es|s)$/;

  function blocked(word) {
    var forms = variants(word);
    for (var i = 0; i < forms.length; i++) {
      var n = forms[i];
      if (index[hash(n)]) return true;
      var stem = n.replace(SUFFIX, '');
      if (stem.length >= 3 && stem !== n && index[hash(stem)]) return true;
    }
    return false;
  }

  /* Splits on whitespace but remembers where each piece sat, so a match can be
     masked in the original text without disturbing anything around it. */
  function pieces(text) {
    var out = [];
    var re = /\S+/g;
    var m;
    while ((m = re.exec(text)) !== null) {
      out.push({ raw: m[0], start: m.index, end: m.index + m[0].length, norm: normalise(m[0]) });
    }
    return out;
  }

  /* Is the run starting at `i` the opening of a known innocent phrase? Windows
     of three to five words, because the exemptions are all short idioms. */
  function exemptAt(words, i) {
    for (var span = 3; span <= 5; span++) {
      if (i + span > words.length) break;
      var joined = '';
      for (var j = i; j < i + span; j++) joined += words[j].norm;
      if (exempt[hash(joined)]) return true;
    }
    return false;
  }

  /* Returns the spans to censor. Runs of three words are tested first, then
     two, then one, so "gas the jews" is caught as a phrase and each of its
     harmless parts is left alone. */
  function findSpans(text) {
    var words = pieces(text);
    var spans = [];
    var taken = {};
    for (var size = 3; size >= 1; size--) {
      for (var i = 0; i + size <= words.length; i++) {
        var j;
        var already = false;
        for (j = i; j < i + size; j++) { if (taken[j]) { already = true; break; } }
        if (already) continue;
        var joined = '';
        for (j = i; j < i + size; j++) joined += words[j].norm;
        if (!joined) continue;
        var hit = size === 1 ? blocked(words[i].raw) : !!index[hash(joined)];
        if (!hit) continue;
        if (exemptAt(words, i)) continue;
        for (j = i; j < i + size; j++) taken[j] = 1;
        spans.push({ start: words[i].start, end: words[i + size - 1].end });
      }
    }
    return spans.sort(function (a, b) { return a.start - b.start; });
  }

  function check(text) {
    var spans = findSpans(String(text == null ? '' : text));
    return { clean: !spans.length, hits: spans.length };
  }

  /* Handles are a single run of characters with no spaces to split on, so
     whole-word matching — the thing that keeps "Scunthorpe" and "raccoon" safe
     in prose — lets "retardlord" straight through. Names get a substring sweep
     instead: every run of the normalised text is tested.

     The five-character floor is what stops that sweep being a menace. Without
     it "coon" matches inside "raccoon" and "spic" inside "spicy"; with it, the
     short slurs are still caught as complete handles, while the long ones are
     caught wherever they are buried. Rejecting a name is also a mild failure —
     the person picks another — where wrongly censoring a review is not. */
  var DENSE_MIN = 5;
  var DENSE_MAX = 20;

  function denseClean(text) {
    var forms = variants(text);
    for (var v = 0; v < forms.length; v++) {
      var s = forms[v];
      for (var len = DENSE_MIN; len <= Math.min(DENSE_MAX, s.length); len++) {
        for (var i = 0; i + len <= s.length; i++) {
          if (index[hash(s.slice(i, i + len))]) return false;
        }
      }
    }
    /* Plus the ordinary word-level pass, which covers the short terms when the
       whole name is one of them. */
    return check(text).clean;
  }

  function checkName(text) {
    return { clean: denseClean(text), hits: denseClean(text) ? 0 : 1 };
  }

  /* One render asks for the same display name dozens of times, so results are
     remembered. Capped rather than unbounded: a long session should not turn
     this into a slow leak. */
  var memo = Object.create(null);
  var memoCount = 0;

  /* Replaces each match with blocks of the same width, so the shape of the
     sentence survives and it is obvious something was removed rather than the
     text simply reading oddly. */
  function mask(text) {
    var s = String(text == null ? '' : text);
    if (!s) return s;
    if (memo[s] !== undefined) return memo[s];

    var spans = findSpans(s);
    var out;
    if (!spans.length) {
      out = s;
    } else {
      out = '';
      var at = 0;
      spans.forEach(function (sp) {
        out += s.slice(at, sp.start);
        out += new Array(Math.max(3, sp.end - sp.start) + 1).join('█');
        at = sp.end;
      });
      out += s.slice(at);
    }

    if (memoCount > 4000) { memo = Object.create(null); memoCount = 0; }
    memo[s] = out;
    memoCount++;
    return out;
  }

  /* Attempts blocked in this browser. Metadata only — the text itself is never
     kept, because storing it would defeat the point of refusing it. */
  var COUNT_KEY = 'postgame.moderation';

  function blockedCount() {
    try {
      return parseInt(window.localStorage.getItem(COUNT_KEY), 10) || 0;
    } catch (e) {
      return 0;
    }
  }

  function recordBlock() {
    try {
      window.localStorage.setItem(COUNT_KEY, String(blockedCount() + 1));
    } catch (e) { /* counting is not worth failing a write over */ }
  }

  PG.moderate = {
    check: check,
    checkName: checkName,
    mask: mask,
    hash: hash,
    normalise: normalise,
    /* Console helpers: does this word trip the filter, and how many attempts
       has this browser refused. */
    test: function (word) { return blocked(word); },
    blockedCount: blockedCount,
    recordBlock: recordBlock,
    size: BLOCKED.length,
    message: 'That looks like a slur or an attack on a group of people. ' +
      'Swearing is fine here — this is not.'
  };
})(window.PG);
