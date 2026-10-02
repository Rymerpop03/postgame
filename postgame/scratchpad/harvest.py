# Harvest 1000 Steam games that are not already in the Postgame catalogue.
#
#   pool      SteamSpy "all" pages, ordered by owners, plus explicit LEGO searches
#   dedupe    against the 1000 rows already in js/games-01..10.js
#   enrich    Steam appdetails for year, developer, genres, platforms, metacritic
#   covers    verify library_600x900.jpg exists so the runtime never 404s
#
# Writes accepted.json; emit.py turns that into the games-*.js files.

import json, os, re, sys, time, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
GAMEJS = r"C:\Users\poke5\OneDrive\Desktop\Testing With Claude Code\directory\postgame\js"
TARGET = 1000
UA = "Mozilla/5.0 (postgame-catalogue-build)"

LEGO_QUERIES = [
    "lego star wars", "lego batman", "lego harry potter", "lego marvel",
    "lego indiana jones", "lego city undercover", "lego jurassic world",
    "lego movie", "lego ninjago", "lego worlds", "lego bricktales",
    "lego builder's journey", "lego brawls", "lego 2k drive", "lego horizon",
    "lego incredibles", "lego dc super-villains", "lego hobbit",
    "lego lord of the rings", "lego pirates of the caribbean",
    "lego dimensions", "lego racers", "lego island", "lego voyagers",
    "lego party", "bionicle", "lego",
]

def get(url, tries=4, timeout=30):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503):
                time.sleep(8 * (attempt + 1))
                continue
            return None
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None

def norm(title):
    t = title.lower().replace("&", " and ")
    t = re.sub(r"[\u2122\u00ae\u00a9]", "", t)
    return re.sub(r"[^a-z0-9]+", "", t)

# ----------------------------------------------------------------- existing

existing = set()
for i in range(1, 11):
    path = os.path.join(GAMEJS, "games-%02d.js" % i)
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith('"'):
                existing.add(norm(line[1:].split("|")[0]))
print("existing catalogue titles:", len(existing), flush=True)

# --------------------------------------------------------------------- pool

pool = []          # [(appid, name, developer, positive, negative)]
seen_ids = set()

def add(appid, name, dev="", pos=0, neg=0):
    appid = int(appid)
    if appid in seen_ids:
        return
    seen_ids.add(appid)
    pool.append((appid, name, dev, pos, neg))

# LEGO first, so they survive the TARGET cut no matter how few owners they have.
for q in LEGO_QUERIES:
    raw = get("https://store.steampowered.com/api/storesearch/?term=%s&cc=us&l=en"
              % urllib.parse.quote(q))
    if not raw:
        continue
    try:
        for item in (json.loads(raw).get("items") or []):
            if item.get("type") == "app":
                add(item["id"], item.get("name", ""))
    except Exception:
        pass
    time.sleep(1.0)
print("pool after LEGO searches:", len(pool), flush=True)

cached = os.path.join(HERE, "steamspy.json")
for page in range(0, 6):
    if page == 0 and os.path.exists(cached):
        raw = open(cached, encoding="utf-8").read()
    else:
        raw = get("https://steamspy.com/api.php?request=all&page=%d" % page, timeout=60)
        time.sleep(62)                     # SteamSpy allows one "all" call a minute
    if not raw:
        print("steamspy page", page, "failed", flush=True)
        continue
    try:
        data = json.loads(raw)
    except Exception:
        continue
    for app in data.values():
        add(app["appid"], app.get("name") or "", app.get("developer") or "",
            app.get("positive") or 0, app.get("negative") or 0)
    print("pool after steamspy page %d: %d" % (page, len(pool)), flush=True)

# Drop anything already in the catalogue, and the obvious non-games, before we
# spend a request on it.
JUNK = re.compile(r"soundtrack|\bost\b|\bdemo\b|art ?book|season pass|dedicated server"
                  r"|\bsdk\b|playtest|closed beta|\bbundle\b|trailer|\beditor\b|toolkit"
                  r"|benchmark|wallpaper|\bmod kit\b|prologue|\bteaser\b", re.I)

candidates = [c for c in pool
              if c[1] and norm(c[1]) not in existing and not JUNK.search(c[1])]
print("candidates to check:", len(candidates), flush=True)

# ------------------------------------------------------------------- enrich

GENRE_MAP = {
    "Action": "Action", "Adventure": "Adventure", "RPG": "RPG",
    "Strategy": "Strategy", "Simulation": "Simulation", "Sports": "Sports",
    "Racing": "Racing", "Massively Multiplayer": "MMO",
}

def clean_text(s, limit=96):
    s = re.sub(r"<[^>]+>", " ", s or "")
    for a, b in (("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"), ("&lt;", "<"),
                 ("&gt;", ">"), ("&nbsp;", " "), ("&rsquo;", "'"), ("&hellip;", "...")):
        s = s.replace(a, b)
    s = s.replace("|", " ").replace('"', "").replace("\\", "")
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",;:-") + "."
    return s

def clean_name(s):
    s = re.sub(r"[\u2122\u00ae\u00a9]", "", s or "")
    s = s.replace("|", "/").replace('"', "'")
    return re.sub(r"\s+", " ", s).strip()

def enrich(cand):
    appid, name, dev, pos, neg = cand
    raw = get("https://store.steampowered.com/api/appdetails?appids=%d&l=en" % appid,
              tries=3, timeout=25)
    if not raw:
        return None
    try:
        node = json.loads(raw).get(str(appid)) or {}
    except Exception:
        return None
    if not node.get("success"):
        return None
    d = node.get("data") or {}
    if d.get("type") != "game":
        return None
    rel = d.get("release_date") or {}
    if rel.get("coming_soon"):
        return None
    m = re.search(r"(19|20)\d{2}", rel.get("date") or "")
    if not m:
        return None
    year = int(m.group(0))
    if year < 1980 or year > 2026:
        return None

    title = clean_name(d.get("name") or name)
    if not title or JUNK.search(title):
        return None

    genres = []
    for g in (d.get("genres") or []):
        mapped = GENRE_MAP.get(g.get("description"))
        if mapped and mapped not in genres:
            genres.append(mapped)
    if not genres:
        genres = ["Action"]

    plat = d.get("platforms") or {}
    platforms = ["PC"] + (["Mac"] if plat.get("mac") else []) + \
                (["Linux"] if plat.get("linux") else [])

    critic = ((d.get("metacritic") or {}).get("score")) or ""
    users = round(pos / (pos + neg) * 100) if (pos + neg) >= 200 else ""
    blurb = clean_text(d.get("short_description")) or "A game on Steam."
    developer = clean_name((d.get("developers") or [dev or "Unknown"])[0])[:48]

    return {
        "appid": appid, "title": title, "year": year, "developer": developer,
        "genres": genres, "platforms": platforms, "critic": critic,
        "users": users, "blurb": blurb,
    }

accepted, taken = [], set()
checked = 0
for cand in candidates:
    if len(accepted) >= TARGET:
        break
    checked += 1
    row = enrich(cand)
    time.sleep(0.55)                       # ~2 requests a second, politely
    if not row:
        continue
    key = norm(row["title"])
    if key in existing or key in taken:
        continue
    taken.add(key)
    accepted.append(row)
    if len(accepted) % 50 == 0:
        print("accepted %d / checked %d" % (len(accepted), checked), flush=True)
        json.dump(accepted, open(os.path.join(HERE, "accepted.json"), "w",
                                 encoding="utf-8"), ensure_ascii=False)

print("enriched: accepted %d from %d checked" % (len(accepted), checked), flush=True)

# ------------------------------------------------------------------- covers

COVER = "https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/%d/library_600x900.jpg"

def has_cover(row):
    try:
        req = urllib.request.Request(COVER % row["appid"],
                                     headers={"User-Agent": UA, "Range": "bytes=0-64"})
        with urllib.request.urlopen(req, timeout=15) as r:
            row["cover"] = r.status in (200, 206)
    except Exception:
        row["cover"] = False
    return row

with ThreadPoolExecutor(max_workers=8) as pool_ex:
    accepted = list(pool_ex.map(has_cover, accepted))

print("with steam cover:", sum(1 for r in accepted if r["cover"]), "of", len(accepted), flush=True)
json.dump(accepted, open(os.path.join(HERE, "accepted.json"), "w", encoding="utf-8"),
          ensure_ascii=False)
print("DONE", flush=True)
