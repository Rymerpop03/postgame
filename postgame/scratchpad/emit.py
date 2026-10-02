# Turn accepted.json into games-11..20.js.
#
# Row: Title|Year|Developer|Genres|Platforms|Critic|Blurb|SteamAppId|SteamUserScore
# The app id is only written when the harvester confirmed a 600x900 capsule
# exists, so the runtime never points at a 404.

import json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
GAMEJS = r"C:\Users\poke5\OneDrive\Desktop\Testing With Claude Code\directory\postgame\js"
PER_FILE = 100

rows = json.load(open(os.path.join(HERE, "accepted.json"), encoding="utf-8"))

import unicodedata

SMART = {"\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
         "\u2013": "-", "\u2014": "-", "\u2015": "-", "\u2026": "...",
         "\u00a0": " ", "\u2122": "", "\u00ae": "", "\u00a9": ""}

def ascii_punct(s):
    for a, b in SMART.items():
        s = s.replace(a, b)
    return s

def deaccent(s):
    """Ragnarok == Ragnar\u00f6k, so accents fold away before comparing."""
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))

CJK = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")

def strip_cjk(title):
    """Steam lists some titles in both scripts: keep the Latin half.

    Only titles that actually contain CJK get the separator cleanup \u2014 running it
    on everything ate the hyphens out of "Years 1-4" and "Super-Villains"."""
    if not CJK.search(title):
        return re.sub(r"\s+", " ", title).strip()
    title = CJK.sub(" ", re.sub(r"[\uff08(][^)\uff09]*[)\uff09]", " ", title))
    return re.sub(r"[\s/\-\u2013\u2014]+", " ", title).strip(" -/")

def norm(title):
    t = deaccent(ascii_punct(title)).lower().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "", t)

# Steam puts the designer or licence on the front; the catalogue does not.
BRAND = re.compile(r"^(sid\s+meier'?s|tom\s+clancy'?s|marvel'?s|disney'?s"
                   r"|american\s+mcgee'?s|peter\s+jackson'?s)\s+", re.I)

def tokens(title):
    """Word-order-insensitive key: Steam sells 'Total War: MEDIEVAL II',
    the catalogue has 'Medieval II: Total War'."""
    t = deaccent(ascii_punct(title)).lower()
    t = EDITION.sub(" ", BRAND.sub("", re.sub(r"\([^)]*\)", " ", t)))
    words = [w for w in re.findall(r"[a-z0-9]+", t) if w not in ("the", "a", "of")]
    return "".join(sorted(words)) if len(words) > 1 else ""

# Steam sells re-releases under their own names, so "DARK SOULS: REMASTERED" and
# "FINAL FANTASY XV WINDOWS EDITION" slip past a plain title comparison even
# though the catalogue already has the game. Strip the edition furniture, then
# compare again.
EDITION = re.compile(
    r"\b(remastered|remaster|definitive|director'?s\s*cut|complete|goty"
    r"|game\s+of\s+the\s+year|windows|enhanced|deluxe|anniversary|redux|special"
    r"|legendary|ultimate|gold|premium|reloaded|classic|online|edition|version"
    r"|scholar\s+of\s+the\s+first\s+sin|the\s+final\s+cut)\b", re.I)

def canon(title):
    t = re.sub(r"\([^)]*\)", " ", title)          # "(Classic, 2005)"
    t = BRAND.sub("", t.strip())                  # "Sid Meier's Civilization VI"
    t = EDITION.sub(" ", t)
    return norm(t)

# Steam store names shout; the catalogue is title case. Only the franchise words
# where the conventional form is unambiguous are touched \u2014 stylised names like
# ULTRAKILL and OMORI are left exactly as their developers write them.
SHOUT = [
    ("LEGO", "Lego"), ("STAR WARS", "Star Wars"), ("FINAL FANTASY", "Final Fantasy"),
    ("DARK SOULS", "Dark Souls"), ("DRAGON BALL", "Dragon Ball"),
    ("NARUTO SHIPPUDEN", "Naruto Shippuden"), ("NARUTO TO BORUTO", "Naruto to Boruto"),
    ("SHINOBI STRIKER", "Shinobi Striker"), ("NINJAGO", "Ninjago"),
    ("MARVEL", "Marvel"), ("ACE COMBAT", "Ace Combat"),
    ("SKIES UNKNOWN", "Skies Unknown"), ("EA SPORTS", "EA Sports"),
    ("DIRECTOR'S CUT", "Director's Cut"), ("REMASTERED", "Remastered"),
    ("WINDOWS EDITION", "Windows Edition"), ("WARHAMMER", "Warhammer"),
    ("FOR HONOR", "For Honor"), ("Ultimate Ninja STORM", "Ultimate Ninja Storm"),
]

def tidy(title):
    for a, b in SHOUT:
        title = re.sub(re.escape(a), b, title)
    return re.sub(r"\s+", " ", title).strip()

NOT_A_GAME = re.compile(r"加速器|accelerator|launcher|wallpaper engine", re.I)

import glob

# Everything already in the catalogue, including the hand-written part 21 —
# but not parts 11-20, which are what this script is about to overwrite.
sources = [p for p in sorted(glob.glob(os.path.join(GAMEJS, "games-*.js")))
           if not re.search(r"games-(1[1-9]|20)\.js$", p)]
print("deduping against:", ", ".join(os.path.basename(p) for p in sources))

existing, existing_canon, existing_tokens = set(), set(), set()
for path in sources:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith('"'):
                t = line[1:].split("|")[0]
                existing.add(norm(t))
                existing_canon.add(canon(t))
                tk = tokens(t)
                if tk:
                    existing_tokens.add(tk)

clean, seen, dropped = [], set(), []
for r in rows:
    r["title"] = tidy(strip_cjk(ascii_punct(r["title"])))
    r["blurb"] = ascii_punct(r["blurb"])
    r["developer"] = ascii_punct(r["developer"])
    title = r["title"]
    key, ckey, tkey = norm(title), canon(title), tokens(title)

    if not title or not r["year"] or not r["developer"]:
        dropped.append((title, "incomplete"))
        continue
    if NOT_A_GAME.search(title):
        dropped.append((title, "not a game"))
        continue
    # A 2026 date with no capsule on Steam's CDN means it has not shipped.
    if r["year"] >= 2026 and not r.get("cover"):
        dropped.append((title, "unreleased"))
        continue
    if key in existing or ckey in existing_canon:
        dropped.append((title, "already in catalogue"))
        continue
    if tkey and tkey in existing_tokens:
        dropped.append((title, "same words as a catalogue title"))
        continue
    if key in seen or ckey in seen or (tkey and tkey in seen):
        dropped.append((title, "duplicate within batch"))
        continue
    seen.add(key)
    seen.add(ckey)
    if tkey:
        seen.add(tkey)
    clean.append(r)

print("usable rows:", len(clean), " dropped:", len(dropped))
for t, why in dropped:
    print("   drop [%s] %s" % (why, t))
if len(clean) < PER_FILE:
    sys.exit("not enough rows to emit")

def field(s):
    return str(s).replace("|", " ").replace('"', "'").strip()

def row_text(r):
    return "|".join([
        field(r["title"]), str(r["year"]), field(r["developer"]),
        ",".join(r["genres"]), ",".join(r["platforms"]),
        str(r["critic"] or ""), field(r["blurb"]),
        str(r["appid"]) if r.get("cover") else "",
        str(r["users"] or ""),
    ])

total = min(len(clean), PER_FILE * 10)
clean = clean[:total]
files = 0
for start in range(0, total, PER_FILE):
    chunk = clean[start:start + PER_FILE]
    part = 11 + start // PER_FILE
    path = os.path.join(GAMEJS, "games-%d.js" % part)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("/* Postgame - game catalogue, part %d\n"
                 "   Harvested from Steam. Rows carry two extra fields: the Steam app id\n"
                 "   (for the 600x900 capsule) and the percentage of Steam reviews that\n"
                 "   are positive. */\n" % part)
        fh.write("window.GAME_ROWS = (window.GAME_ROWS || []).concat([\n")
        for i, r in enumerate(chunk):
            comma = "," if i < len(chunk) - 1 else ""
            fh.write('"%s"%s\n' % (row_text(r), comma))
        fh.write("]);\n")
    files += 1
    print("wrote", os.path.basename(path), len(chunk), "rows")

with_cover = sum(1 for r in clean if r.get("cover"))
print("emitted %d rows across %d files; %d with a verified Steam capsule"
      % (total, files, with_cover))
legos = [r["title"] for r in clean if "lego" in r["title"].lower()]
print("lego titles included: %d" % len(legos))
for t in sorted(legos):
    print("   ", t)
