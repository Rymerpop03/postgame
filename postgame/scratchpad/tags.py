# Second pass: upgrade the coarse Steam genres on accepted.json using SteamSpy's
# user tags, so the new rows carry the same fine-grained vocabulary as the
# hand-written catalogue (Metroidvania, Souls-like, Roguelike, City Builder...)
# rather than a thousand games all tagged "Action".

import json, os, re, time, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
UA = "Mozilla/5.0 (postgame-catalogue-build)"

# SteamSpy tag -> the genre name already used in the catalogue.
TAGS = {
    "Souls-like": "Souls-like", "Metroidvania": "Metroidvania",
    "Roguelike": "Roguelike", "Roguelite": "Roguelike",
    "Rogue-like": "Roguelike", "Rogue-lite": "Roguelike",
    "FPS": "FPS", "First-Person Shooter": "FPS",
    "Third-Person Shooter": "TPS", "Shoot 'Em Up": "Shoot 'em up",
    "Bullet Hell": "Shoot 'em up", "Twin Stick Shooter": "Shooter",
    "Shooter": "Shooter", "Battle Royale": "Battle Royale",
    "Hero Shooter": "Hero Shooter", "Looter Shooter": "Shooter",
    "Platformer": "Platformer", "2D Platformer": "Platformer",
    "3D Platformer": "Platformer", "Precision Platformer": "Platformer",
    "Puzzle": "Puzzle", "Puzzle Platformer": "Puzzle",
    "Horror": "Horror", "Survival Horror": "Horror", "Psychological Horror": "Horror",
    "Survival": "Survival", "Open World Survival Craft": "Survival",
    "Open World": "Open World", "Exploration": "Exploration",
    "RPG": "RPG", "JRPG": "JRPG", "CRPG": "RPG", "Action RPG": "Action RPG",
    "Turn-Based Combat": "Turn-Based", "Turn-Based": "Turn-Based",
    "Turn-Based Strategy": "Strategy", "Turn-Based Tactics": "Tactics",
    "Tactical RPG": "Tactics", "Tactical": "Tactical",
    "Strategy": "Strategy", "RTS": "RTS", "Real-Time Strategy": "RTS",
    "Grand Strategy": "Strategy", "4X": "Strategy",
    "City Builder": "City Builder", "Colony Sim": "Colony Sim",
    "Base Building": "Sandbox", "Management": "Management",
    "Economy": "Management", "Automation": "Simulation",
    "Simulation": "Simulation", "Life Sim": "Life Sim",
    "Farming Sim": "Farming", "Farming": "Farming",
    "Racing": "Racing", "Driving": "Racing", "Sports": "Sports",
    "Fighting": "Fighting", "2D Fighter": "Fighting", "3D Fighter": "Fighting",
    "Beat 'em up": "Beat 'em up", "Hack and Slash": "Hack and Slash",
    "Stealth": "Stealth", "Immersive Sim": "Immersive Sim",
    "Visual Novel": "Visual Novel", "Story Rich": "Adventure",
    "Point & Click": "Point and Click", "Adventure": "Adventure",
    "Interactive Fiction": "Adventure", "Walking Simulator": "Adventure",
    "Card Game": "Card Game", "Deckbuilding": "Card Game",
    "Card Battler": "Card Game", "Auto Battler": "Auto Battler",
    "MOBA": "MOBA", "MMORPG": "MMO", "Massively Multiplayer": "MMO",
    "Co-op": "Co-op", "Online Co-Op": "Co-op", "Local Co-Op": "Co-op",
    "Party Game": "Party", "Social Deduction": "Social Deduction",
    "Rhythm": "Rhythm", "Music": "Rhythm",
    "Tower Defense": "Tower Defence", "Dungeon Crawler": "Dungeon Crawler",
    "Detective": "Detective", "Mystery": "Mystery",
    "Arcade": "Arcade", "Mech": "Mech", "Sandbox": "Sandbox",
    "Action": "Action", "Action-Adventure": "Action Adventure",
    "Run and Gun": "Run and Gun", "Endless Runner": "Endless Runner",
    "VR": "VR", "Rail Shooter": "Rail Shooter", "Idler": "Simulation",
}

rows = json.load(open(os.path.join(HERE, "accepted.json"), encoding="utf-8"))
print("rows to tag:", len(rows), flush=True)

def fetch_tags(appid):
    url = "https://steamspy.com/api.php?request=appdetails&appid=%d" % appid
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            t = d.get("tags")
            return t if isinstance(t, dict) else {}
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                time.sleep(5 * (attempt + 1))
                continue
            return {}
        except Exception:
            time.sleep(2)
    return {}

upgraded = 0
for n, r in enumerate(rows):
    tags = fetch_tags(r["appid"])
    time.sleep(1.05)                       # SteamSpy: one request a second
    if not tags:
        continue
    ranked = sorted(tags.items(), key=lambda kv: -kv[1])
    picked = []
    for name, _votes in ranked:
        g = TAGS.get(name)
        if g and g not in picked:
            picked.append(g)
        if len(picked) == 3:
            break
    if picked:
        # Keep a coarse Steam genre only if the tags gave us nothing better.
        r["genres"] = picked
        upgraded += 1
    if (n + 1) % 100 == 0:
        print("tagged %d/%d (upgraded %d)" % (n + 1, len(rows), upgraded), flush=True)
        json.dump(rows, open(os.path.join(HERE, "accepted.json"), "w",
                             encoding="utf-8"), ensure_ascii=False)

json.dump(rows, open(os.path.join(HERE, "accepted.json"), "w", encoding="utf-8"),
          ensure_ascii=False)
print("DONE - upgraded genres on %d of %d rows" % (upgraded, len(rows)), flush=True)
