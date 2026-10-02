# Postgame

**Log the games you play.** A Letterboxd-style diary for video games — rate out of
five stars (half stars included), write reviews, keep a want-to-play list, and
browse a catalogue of 2,000 games.

Built with plain HTML, CSS and JavaScript. No build step, no dependencies, no
server required.

## Running it

Open `index.html` in a browser. That's it — everything (catalogue, accounts,
diary entries) is loaded from local files and stored in `localStorage`.

If you'd rather serve it over HTTP, use the included server rather than
`python -m http.server` — it sends `Cache-Control: no-store`, so edits show up
instead of the browser quietly serving you last week's JavaScript:

```bash
python serve.py
```

Then visit <http://localhost:8130>. Script tags also carry a `?v=` query, so a
changed file can't be served from a stale cache even on a plain static host —
bump that number when you edit the JS or CSS.

## Test accounts

Ten demo members come pre-loaded with 119 diary entries and 82 reviews between
them — including ten games mid-playthrough and nine they gave up on — so the
site looks lived-in from the first visit. Their hours are generated from genre
and release era, the same seed fiction as their ratings, so the totals look
plausible rather than empty. **Every demo account uses the password
`postgame`:**

| Username | Member | Taste |
| --- | --- | --- |
| `pixelvagrant` | Ash Moreau | Souls games, parry windows |
| `quinnbyte` | Quinn Ledger | Big open worlds, co-op, Balatro |
| `neonharbor` | Rae Okonkwo | Immersive sims, level design |
| `sixteenbit` | Tomas Beier | Cartridges, 1985–1999 |
| `haloweenie` | Devin Park | Shooters and co-op |
| `moonlitmage` | Ingrid Salas | JRPGs and 200-hour saves |
| `framerate_fran` | Fran Ito | Fighting games, frame data |
| `couchcoop_kev` | Kev Duarte | Party games, second controller |
| `hollowbee` | Bee Nakamura | Metroidvanias |
| `glitchwitch` | Nour Haddad | Roguelikes and speedruns |

You can also create your own account from the sign-in page — it's stored in the
browser alongside everything else.

> **These are fake accounts in a local demo.** Credentials sit unencrypted in
> `localStorage` and are never sent anywhere. Don't reuse a real password. To
> wipe everything, run `PG.store.reset()` in the console and reload.

## What's in it

- **2,000 games** with year, developer, genres, platforms, critic score and a
  one-line description, spanning 1972 to 2025 across 70 genres and 38 platforms.
  Half the catalogue is hand-written; the other half was harvested from Steam.
- **Real cover art** for almost all of them — 844 straight from Steam's CDN at a
  uniform 600×900, the rest fetched from Wikipedia on demand. Anything without
  keeps its generated cover.
- **The complete Lego series**, all 53 of them, from Lego Island (1997) to Lego
  Party! (2025) — including the twenty-four that never came to Steam.
- **Diary entries** — half-star ratings, a play state, hours played, the
  platform you played it on, a date, a like, a replay flag and an optional
  review, all editable and removable.
- **Play states** — *playing*, *finished* or *abandoned*, because games are not
  the binary that films are. Each gets its own shelf on a profile, its own stat,
  and a badge on the cover. Dropped-at-hour-six is a real data point.
- **Rate without opening anything** — a star strip sits on the game page and on
  every grid tile. Hover a cover, click a star, done; it saves immediately and
  every other copy of that game's strip on screen moves with it.
- **Comment threads on reviews.** Every review carries a conversation: expand it
  from the review's footer, reply to anyone, and `@handle` becomes a link when it
  names a real member. Threads are collapsed by default — the activity feed is
  twenty reviews long and would be unreadable otherwise — and they read oldest
  first, because a conversation runs top to bottom even though the diary feeds
  run newest first. You can delete your own comments, and the author of a review
  can delete anything on theirs. Ctrl/Cmd+Enter posts. Fifty seeded comments
  across eighteen threads mean the feature has something to show on a first
  visit.
- **Community scores** — every game has a rating distribution and histogram.
  Real member entries are added on top of a stable baseline anchored to the
  critic score, so your rating visibly moves the average.
- **Browse** — filter by title/studio, genre, platform and decade; sort by
  popularity, community rating, most logged, critic score, year or title.
- **Search** — instant dropdown from the header, with arrow-key navigation.
  Press <kbd>/</kbd> anywhere to focus it. The Members page has its own search,
  matching on handle, display name or bio.
- **Profiles** — five headline figures over a play-state bar, plus
  currently-playing, favourites, recent diary, reviews, gave-up-on and
  want-to-play shelves. The live star strips only appear on your own shelves;
  someone else's ratings are theirs to change.
- **Settings**, from the account menu: edit your display name, bio and avatar,
  and switch between the light and dark themes. Profile edits apply to the demo
  members too — they are stored as an override layer, since the seeded records
  themselves are rebuilt from `data.js` on every load.
- **A profile picture** you can actually set, with no server to upload it to.
  The file is centre-cropped and downscaled to 192px in a canvas and kept as a
  data URL, which turns a 500KB photo into about 5KB — small enough to sit in
  `localStorage` beside everything else. Or pick one of ten avatar colours and
  keep the lettered version.
- **Light and dark themes.** Dark is the default; the choice is *System*, *Light*
  or *Dark*, and *System* follows the OS live. The preference belongs to the
  browser rather than the account, so it works signed out.
- **Sound.** Two short cues, on the two actions that commit something: a rising
  pair of notes when you follow a member (falling when you unfollow) and a warm
  arpeggio when you save a diary entry. Nothing plays without a click behind it,
  and Settings has an on/off switch.
- **A content filter** on everything members write — reviews, comments,
  messages, display names and bios. It looks for slurs and attacks on people for
  who they are, not for rudeness: "this boss is fucking unfair" is normal writing
  about a video game and goes straight through. Flagged text is refused at the
  point of writing *and* masked at the point of display, so the guarantee holds
  for anything already in storage. There is no UI for it — it is always on,
  there is nothing to configure, and the only thing a settings panel would
  reliably communicate is where the edges are. `PG.moderate.blockedCount()` and
  `PG.moderate.test('word')` are there in the console when you want them.
- **Direct messages**, but only between members who follow *each other*.
  Following someone does not put you in their inbox — the compose box opens when
  the follow goes both ways, and closes again if either side unfollows, without
  deleting what was already said. Threads live under **Messages** in the account
  menu, which carries an unread count, and mutuals get a Message button on their
  profile. Ctrl/Cmd+Enter sends. **New message** opens a searchable picker of
  everyone who follows you back — searching name, handle or bio — rather than
  padding the conversation list with a row per person you have never written to.
- **Taste match.** Every member's profile shows how closely your library lines
  up with theirs — a percentage, a plain-English band, the games you both rated
  highly, and the ones you flatly disagree on with both scores side by side. The
  Members page opens with **Closest to your taste**: the members you are not
  following yet, ranked by the same measure, and every card in the full list
  carries its match.
- **Following.** Follow any member from their profile or straight from the
  members list, and the Activity page gains an **Everyone / Following** switch
  that narrows the feed to the diaries you actually care about. Your own entries
  stay out of it — the point is what everyone *else* is playing. Profiles carry
  follower and following counts that open their own pages, cards are tagged
  **Follows you**, and the home page grows a "From people you follow" section
  once your circle has logged something. The demo members come pre-wired into a
  taste-clustered graph of 37 follows, so the feed is populated on a first visit
  rather than empty.
- **One Activity feed** for the whole site. Reviews used to be a separate page,
  but every review *is* a diary entry, so it was the same list rendered twice.
  It is now one page with a filter (everything / reviews only) and a sort
  (newest / most liked) — which also gives two views neither page offered.
  Rating-only entries render as compact feed rows; written reviews get the full
  card. `#/reviews` still redirects there, so old links keep working.

## How it's put together

```
index.html            page shell, icon sprite, script order
css/styles.css        design tokens and every component
assets/logo.svg       full logo lockup
assets/favicon.svg    the mark on its own
js/games-01..21.js    the catalogue, 100 pipe-delimited rows per file
                      01-10 hand-written, 11-20 harvested from Steam,
                      21 hand-written (Lego classics, console/mobile exclusives)
js/theme.js           light/dark preference and resolution
js/sound.js           the interface cues, synthesised at play time
js/moderation.js      the content filter: hashed list, matching, masking
test/filter-test.html open in a browser to run the filter's test suite
js/data.js            parses rows, builds indexes, holds the seeded community
js/art.js             fetches and caches real cover art
js/store.js           accounts, profiles, diary entries, comments, follows,
                      likes, localStorage
js/ui.js              markup helpers, toasts, modal
js/app.js             hash router, views, event handling
```

A few deliberate choices:

- **Hash routing** (`#/game/elden-ring`) and plain `<script>` tags instead of ES
  modules, so the site works from `file://` as well as over HTTP.
- **One light-theme block, not two.** The stored preference can be *system*, but
  only the *resolved* value (`light` or `dark`) is ever written to the document,
  so the stylesheet needs a single `[data-theme="light"]` block rather than a
  copy of it behind a `prefers-color-scheme` media query. A small inline script
  in `<head>` resolves it before the first paint — the rest of the app loads at
  the end of `<body>`, so without that a light-theme reader gets a dark flash on
  every page load.
- **The filter has a test suite.** Open `test/filter-test.html` — 103 checks in
  nine groups, run against the real module through the same public functions the
  site uses, with a pass/fail table and a verdict at the top. Reload to re-run.
  Roughly a third of it is ordinary writing and near-misses that must *not* be
  caught, because over-blocking is the failure mode that actually bites. The
  hostile inputs are base64 in the fixture and redacted in the output, for the
  same reason the word list is hashed; each case is described by the rule it
  tests, which is the part you need when one goes red.
- **The filter's word list is stored as hashes, not text.** A file containing a
  few hundred slurs in plain sight is unpleasant to open, easy to leak into a
  search index, and the sort of thing that gets quoted out of context — so only
  opaque keys are kept. `PG.moderate.hash('term')` in the console produces a key
  to paste in, which means adding a term never requires typing one into the
  source. The trade-off is that the list cannot be read back;
  `PG.moderate.test('word')` answers whether a given word is caught.
- **Two matching rules, because prose and handles are different problems.**
  Free text matches on whole words, which is what keeps *Scunthorpe*, *raccoon*
  and *assassin* out of trouble. A username has no spaces to split on, so
  `retardlord` sails past that rule — names get a substring sweep instead, with a
  five-character floor so *raccoon_fan* and *spicy_takes* survive it. Rejecting a
  name is a mild failure; wrongly censoring a review is not, and the two rules
  are tuned to that asymmetry.
- **Evasion is handled by normalising, not by listing variants.** Leetspeak,
  accents, separators (`r-e-t-a-r-d`), padded letters (`reeeetard`) and Cyrillic
  look-alike characters all fold down to the same key before matching, so the
  list holds one canonical spelling per term rather than a combinatorial pile.
- **Filters fail in two directions and one of them is worse.** Over-blocking is
  the failure that makes a site unusable and lands hardest on the people the
  filter exists to protect, so the list deliberately leaves out reclaimed words
  like *queer*, ambiguous ones like *poof* and *fairy* (this is a games site),
  and honorifics like *Hajji*. Where a slur has a genuine idiomatic use, the
  phrase is exempted rather than the word dropped: *a chink in the armour* is
  allowed, the word alone is not.
- **The sounds are synthesised, not shipped.** Three cues as audio files would be
  tens of kilobytes of binary plus a build step to keep them in sync; the whole
  of `sound.js` is under 3KB and the cues are edited by changing numbers. Same
  reasoning as the generated cover art. The AudioContext is built on first play
  rather than on load — browsers refuse to start one outside a user gesture, and
  one created early just sits suspended — and each note is ramped in and out of
  a near-zero floor, because a gain that jumps from silence is an instantaneous
  edge and speakers reproduce that as a click.
- **Themed by token, with two deliberate exceptions.** Every rule reads a
  variable rather than a literal colour, including the hover tints — in dark they
  lift a surface, in light they have to sink into it. The exceptions are anything
  sitting *on* cover art (the playing/abandoned badges, the poster captions),
  which stays keyed to the artwork's own darkness in both themes, and the brand
  gradient, which is the logo. Where that purple has to carry text it uses
  `--accent` instead, because `#8f7cff` on white is nowhere near readable.
- **Two layers of cover art.** Every cover is drawn first as a deterministic
  gradient, pattern and typographic treatment derived from a hash of the title,
  using CSS container queries so one component scales from a 30px search result
  to a full-size poster. Real box art then fades in over the top when `art.js`
  finds it. The generated cover is the loading state *and* the permanent
  fallback, so nothing is ever blank.
- **Seeded randomness.** Cover hues, rating spreads and review like counts all
  come from a hash of the title, so they never change between reloads.
- **A whole is not a peer of its parts.** The profile used to show eight equal
  boxes, three of which — finished, playing, abandoned — are a breakdown of a
  fourth, games logged. Rendered identically they read as unrelated metrics, and
  eight two-word labels wrapped to two lines at every width below 1220px. It is
  now five headline figures with one-word labels that cannot wrap, over a
  proportion bar for the play states, so "does this person finish what they
  start" is answerable at a glance. Zero-count states are dropped from the bar
  rather than shown as an empty slice.
- **The messaging gate lives in the store, not the view.** Hiding the compose
  box would be decoration; `sendMessage` re-checks the mutual follow on every
  call and refuses regardless of how it was reached. Unread state is a
  read-watermark per conversation rather than a flag per message, so opening a
  thread is a single write no matter how far behind you were.
- **How the taste match is calculated.** Three ingredients, in order of how much
  they are trusted. **Rating agreement** on the games both members have played:
  each shared game scores on how far apart the two ratings are, with two and a
  half stars counting as total disagreement. **Genre affinity** — the cosine
  similarity of what each member reaches for, weighted by how much they liked
  it — acts as the prior, because in a 2,000-game catalogue two people can have
  compatible taste and nothing in common. Agreement is pulled toward that prior
  by how thin the overlap is, so one shared five-star game cannot claim a
  perfect match. Finally the **whole score is pulled toward 50%** by the size of
  the smaller library, reaching full confidence at eight ratings: without that
  step a new account that rates one metroidvania scored 91% against the
  metroidvania obsessive, because a one-game genre vector points in exactly one
  direction. Across the ten demo members the result spans 44–82%, and the pairs
  at each end are the ones you would pick by hand.
- **The seeded diary needed crossover to compare.** Each member was written
  logging only their own niche, which is true to life but left **40 of the 45
  member pairs sharing zero games** — nothing for a comparison to compare. Ten
  crossover hits (Portal 2, Breath of the Wild, Hades, Celeste…) now run through
  the cast, rated to disagree along the lines those members already disagree on.
  Every pair now shares between four and nine games.
- **No reverse index for followers.** The follow graph is stored one row per
  member — who *they* follow — and `followersOf` works the other direction out on
  demand. With a graph this size that is a walk of ten short lists, and it can
  never fall out of step with the forward edges the way a cached reverse index
  would.
- **Seeded content is referenced by name, not by index.** Diary entries get ids
  like `seed-14` from their position in the table, so the comments table points
  at a review by *author and game title* and resolves the id at load. Referencing
  `seed-14` directly would silently reattach every comment to the wrong review
  the first time someone reorders the entries; anything that fails to resolve
  warns in the console rather than vanishing.

## Where the cover art comes from

`js/art.js` asks the public Wikipedia API for each game's lead image. It is
entirely optional: no key, no account, and if the request fails — offline, rate
limited, or blocked by a browser that refuses cross-origin calls from
`file://` — the generated covers simply stay. (Chrome does allow it from
`file://`, so opening `index.html` directly still gets you real art.)

Three passes per game, stopping at the first hit:

1. an exact article lookup, batched 30 titles per request
2. a search for the same title, for anything that missed
3. a read of the article's file list, for the pages Wikipedia has not indexed
   for images

That third pass is the fussy one. It only accepts a file that names itself as
cover art — "first image in the article" gives you a photo of a booth at E3 —
and falls back to a wordmark for the dozen huge games that have no box art on
Wikipedia at all (Minecraft, Roblox, Undertale, RuneScape). Candidates are then
ranked, because an article can list a dozen logos: filenames are scored on how
much they contain beyond the game's own name, then on recency, so *Minecraft
game logo 2023* wins over *Minecraft Marketplace logo* and Roblox's current
wordmark wins over its 2004 one. Anything matching a film or soundtrack is
thrown out, or half this catalogue would end up with its movie adaptation's
poster.

That leaves four games — Dwarf Fortress, Geometry Dash, The King of Fighters '98
and Oxenfree — with no usable image anywhere on Wikipedia. They keep their
generated covers.

**Fitting.** Only about two thirds of what comes back is actually box-art
shaped; the rest is wide key art, banners and wordmarks, some as extreme as 3:1.
Filling a 3:4 frame with those would crop them to a meaningless middle slice,
but letterboxing everything that isn't exactly 3:4 leaves most of the grid
looking small and weak. So `art.js` measures each image and spends a **crop
budget**: fill the frame edge to edge whenever the trim costs less than 35% of
one dimension — which is what any storefront does to key art — and letterbox
over a blurred, darkened copy of itself when it would cost more.

A wordmark gets a budget of 10% instead, because clipping the ends off
"MINECRAFT" reads as broken rather than as a crop; `art.js` spots those from the
filename. Most covers therefore fill their frame edge to edge, and banners too
wide to trim are letterboxed over a blurred, darkened copy of themselves with
their title caption kept. The blur radius is set in container units so it scales
with the cover instead of smearing a 44px thumbnail.

**Wordmarks get their own treatment.** A handful of enormous games — Minecraft,
Fortnite, Roblox, Undertale — have no cover art anywhere, only a logo several
times wider than it is tall (Minecraft's is nearly 6:1). Letterboxing one over a
blurred copy of itself buries it in noise, so those sit on the generated gradient
instead: pattern showing, a drop shadow to lift the logo off the background, and
the caption kept underneath. Clean, high contrast, and already keyed to that
game's colour.

Two of them are **pinned by hand** in `art.js`. Minecraft's Wikipedia article
sits next to a film of the same name, and leaving that to a search is asking for
trouble — it resolved to *A Minecraft Movie*'s poster once already.

The first screenful of any page is fetched immediately and the rest lazily via
`IntersectionObserver`. Results are cached in `localStorage`, so a page you have
already visited fills in instantly and never re-requests. A failed request is
retried twice with a backoff before the whole thing gives up, so one blip does
not cost every remaining cover on the page.

Only game titles leave the browser, and only to `en.wikipedia.org`. Cover images
are hotlinked from Wikipedia rather than redistributed with the project; they
remain the property of their publishers. Run `PG.art.forget()` in the console
and reload to clear the image cache, or `PG.art.disable()` to turn the whole
thing off for the session.

## Adding more games

Append rows to any `js/games-*.js` file — or add a `games-11.js` and a matching
`<script>` tag. The format is pipe-delimited:

```
Title|Year|Developer|Genre,Genre|Platform,Platform|CriticScore|One-line description
```

Steam-sourced rows add two optional fields — the Steam app id and the percentage
of Steam reviews that are positive:

```
Title|Year|Developer|Genres|Platforms|Critic|Description|SteamAppId|SteamPositive%
```

The app id is what makes the cover free: Steam serves a 600×900 capsule at a
predictable URL, so those covers need no lookup at all. The positive percentage
feeds the community rating curve for games Metacritic never scored (it gets its
own curve — a Steam "94% positive" is not 94/100).

**It is also the best cover fix available.** A row with no app id falls through to
Wikipedia, and a title carrying store furniture — *Call of Duty: Advanced Warfare
- Gold Edition* — matches no article, so the search pass takes over and can drift
somewhere unrelated: that one landed on *Call of Duty Zombies* and wore a
screenshot from *Night of the Living Dead*. Finding the real app id (209650) fixed
it outright. 77 rows lost their app id when the harvester's capsule check failed,
and they are the ones most likely to be wearing the wrong cover.

Everything else — slugs, cover art, filters, taxonomies, rating curves — is
derived automatically.

## How the Steam half was built

Not by hand. `scratchpad/harvest.py` pulled a pool of ~6,000 apps from SteamSpy
ordered by owners, plus targeted searches for every Lego game, then queried
Steam's `appdetails` for each candidate's release year, developer, platforms and
Metacritic score. A second pass swapped Steam's four coarse genres ("Action",
"Adventure"…) for its user tags, so the harvested rows carry the same
fine-grained vocabulary as the hand-written ones — Metroidvania, Souls-like,
Colony Sim. Covers were verified with a range request before the app id was
written, so the runtime never points at a 404.

Deduping against the existing catalogue was the hard part. A plain title
comparison misses almost everything that matters, so titles are compared three
ways: normalised, canonicalised (edition furniture, designer prefixes and
parentheticals stripped), and as a sorted bag of words. That caught 78 rows the
naive check waved through — *Dark Souls: Remastered*, *Skyrim Special Edition*,
*Sid Meier's Civilization VI*, *God of War Ragnarök* (accent), *Total War:
MEDIEVAL II* (reordered), and a Chinese network accelerator claiming to be a
game.

## Logo

The mark is a d-pad inside a speech bubble — the conversation that starts once
the credits roll. Postgame: the part of a game that happens after you put the
controller down.

The pads are four separate squares rather than a solid cross. A cross reads as a
medical symbol, and the mark started life with a centred play triangle, which
put a dark rounded rectangle around a centred white triangle — YouTube's mark
with the colour changed. The d-pad keeps the bubble and the gradient and drops
the resemblance.

---

The catalogue is factual to the best of our knowledge, but it was assembled by
hand — if a release year or studio credit looks wrong, it probably is. Not
affiliated with Letterboxd.
