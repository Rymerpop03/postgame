"""Generate app/security/data/common_passwords.txt.

Committed as a generator plus its output rather than as an opaque blob, so that what the
list contains is reviewable and reproducible. Run it after editing the base vocabulary:

    python tools/build_password_list.py

**What this is not.** It is not the real top-100,000 breach corpus that BACKEND-PLAN.md
Phase 5 asks for. Fetching that means downloading a large file from a third party, which is
a decision for a person rather than something to do quietly. What it *is* is a mechanical
expansion of the base words people actually choose across the mutations people actually
apply — a trailing year, a digit run, an exclamation mark, a leetspeak substitution. Those
mutations are why a naive "is it a dictionary word" check fails: almost nobody types
`dragon`, and a great many people type `Dragon2024!`.

The consuming end is honest about the gap: `app/security/breached.stats()` reports the
entry count, and `deployment_warnings()` says in production that this is not the full
corpus. Point PG_PASSWORD_LIST at the real one when there is one.

Note the interaction with the policy: a 12-character minimum already refuses most of the
short classics outright, so the entries that earn their place here are the long ones —
`password123456`, `qwertyuiop123`, `iloveyou2024!`. Generating them is the point.
"""

from __future__ import annotations

import sys
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "app" / "security" / "data"
OUT = DATA / "common_passwords.txt"

# The classics, kept verbatim: these are the literal strings that top every published
# frequency list, and several are not derivable from any word list.
LITERAL = """
123456 123456789 12345678 1234567 12345 1234 123 111111 000000 121212 123123 654321 666666
555555 777777 888888 999999 112233 123321 789456 456789 1234567890 987654321 0123456789
11111111 00000000 22222222 12341234 10101010 147258369 159753 963852741 qwerty qwertyuiop
qwertyui qwerty123 qwerty1 qwe123 qweasd qweasdzxc 1q2w3e 1q2w3e4r 1q2w3e4r5t 1qaz2wsx
1qazxsw2 zaq12wsx qazwsx qazwsxedc asdfgh asdfghjkl zxcvbn zxcvbnm zaq1zaq1 poiuytrewq mnbvcxz
lkjhgfdsa 1qaz2wsx3edc qwertz azerty password passw0rd p@ssword p@ssw0rd pa55word passwort
password1 password12 password123 password1234 password12345 password123456 passwords iloveyou
letmein welcome trustno1 whatever princess sunshine shadow master monkey dragon football
baseball superman batman starwars pokemon minecraft computer internet freedom hello secret
access login admin administrator root guest test testing changeme default temporary temp123
abc123 abcd1234 abcdefgh a1b2c3d4 qazqaz asdasd zxczxc trustme letmein1 opensesame
""".split()  # noqa: SIM905 - a word list stays a word list

# Base words. Anything people put in front of a year or a digit run: names, teams, brands,
# animals, colours, months, the vocabulary of affection and profanity. Deliberately mixed,
# because breach corpora are.
BASE = """
password pass secret admin login user welcome hello money love lovely loveme iloveyou angel
angels baby babygirl babyboy sweetie honey darling kitten puppy princess prince queen king
boss chief captain hunter killer ninja samurai warrior soldier ranger sniper shadow phoenix
dragon falcon eagle tiger tigers lion lions wolf wolves bear bears panther panthers cobra
viper raptor hawk shark dolphin monkey rabbit turtle spider scorpion summer winter spring
autumn january february march april may june july august september october november december
monday friday sunday weekend holiday birthday christmas easter halloween newyear michael
jennifer jessica ashley matthew joshua daniel david james robert john william richard thomas
charles christopher andrew anthony joseph mark steven paul kevin brian george edward ronald
timothy jason jeffrey ryan jacob gary nicholas eric stephen jonathan larry justin scott
brandon benjamin samuel gregory patrick alexander jack dennis jerry tyler aaron henry douglas
peter adam nathan zachary walter kyle harold carl jeremy keith roger gerald ethan arthur terry
christian sean lawrence austin joe noah jesse logan elizabeth barbara susan margaret dorothy
lisa nancy karen betty helen sandra donna carol ruth sharon michelle laura sarah kimberly
deborah amy angela melissa brenda anna rebecca virginia kathleen pamela martha debra amanda
stephanie carolyn christine marie janet catherine frances ann joyce diane alice julie heather
teresa doris gloria evelyn jean cheryl mildred katherine joan ashley judith rose janice kelly
nicole judy christina kathy theresa beverly denise tammy irene jane lori rachel marilyn andrea
kathryn louise sara anne jacqueline wanda bonnie julia ruby lois tina phyllis norma paula
diana annie lillian emily robin peggy crystal gladys rita dawn connie florence tracy edna
tiffany carmen rosa cindy grace wendy victoria edith kim sherry sylvia josephine thelma
shannon sheila ethel ellen elaine marjorie carrie charlotte monica esther pauline emma juanita
anita rhonda hazel amber eva debbie april leslie clara lucille jamie joanne eleanor valerie
danielle megan alicia suzanne michele gail bertha darlene veronica jill erin geraldine lauren
cathy joann lorraine lynn sally regina erica beatrice dolores bernice audrey yvonne annette
samantha marion dana stacy ana renee ida vivian roberta holly brittany melanie loretta yolanda
jeanette laurie katie kristen vanessa alma sue elsie beth jeanne vicki carla tara rosemary
eileen terri gertrude lucy tonya ella stacey wilma gina kristin jessie natalie agnes vera
willie charlene bessie delores melinda pearl arlene maureen colleen allison tamara joy georgia
constance lillie claudia jackie marcia tanya nellie minnie marlene heidi glenda lydia viola
courtney marian stella caroline dora jo vickie mattie maria susan chelsea hannah abigail
madison olivia sophia isabella charlotte amelia liverpool chelsea arsenal everton tottenham
united city rangers celtic barcelona madrid juventus milan roma napoli bayern dortmund ajax
porto benfica psg lakers celtics knicks bulls heat spurs warriors rockets yankees redsox
dodgers giants cowboys patriots steelers packers eagles falcons broncos raiders chiefs saints
vikings ravens seahawks football soccer baseball basketball hockey cricket tennis rugby boxing
wrestling golf skating surfing skiing fishing hunting camping climbing running cycling
swimming ferrari porsche mercedes corvette mustang camaro harley yamaha honda toyota nissan
subaru ford chevy dodge audi jaguar bentley lambo tesla ducati kawasaki suzuki nintendo
playstation xbox steam discord twitch youtube tiktok twitter instagram facebook snapchat
netflix spotify amazon google apple samsung microsoft paypal bitcoin ethereum minecraft
fortnite roblox pokemon zelda mario sonic halo doom skyrim overwatch valorant starwars
startrek matrix batman superman spiderman ironman avengers hogwarts gandalf frodo aragorn
potter hermione voldemort naruto goku vegeta pikachu charizard chocolate cookie cookies muffin
cupcake pizza burger coffee whiskey vodka beer wine banana apple orange cherry peach mango
lemon strawberry pineapple watermelon purple orange yellow silver golden crimson scarlet
violet indigo turquoise flower flowers rose roses daisy lily tulip orchid jasmine sunflower
guitar piano violin drums music song singer dancer artist writer poet computer internet
network server database keyboard monitor laptop desktop mobile freedom liberty justice peace
hope faith trust dream dreams magic wonder soccer12 mustang1 shadow1 dragon1 monkey1 princess1
sunshine1 iloveyou1 qwerty qwertyui asdfjkl trustno1 whatever nothing anything something
everything buster bailey charlie cooper daisy lucky max molly rocky sadie toby zeus bella luna
maggie sophie chloe ruby coco milo oscar simba nala mittens whiskers fluffy patches
""".split()  # noqa: SIM905 - a word list stays a word list

YEARS = [str(y) for y in range(1970, 2031)]
SHORT_YEARS = [f"{y % 100:02d}" for y in range(1980, 2031)]

DIGIT_SUFFIXES = [
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "7",
    "8",
    "9",
    "0",
    "01",
    "02",
    "07",
    "11",
    "12",
    "21",
    "22",
    "23",
    "42",
    "69",
    "77",
    "88",
    "99",
    "00",
    "007",
    "111",
    "123",
    "222",
    "321",
    "333",
    "420",
    "555",
    "666",
    "777",
    "888",
    "999",
    "000",
    "1234",
    "4321",
    "1111",
    "0000",
    "2020",
    "12345",
    "54321",
    "123456",
    "111111",
    "654321",
    "1234567",
    "12345678",
    "123456789",
    "1234567890",
]

SYMBOL_SUFFIXES = ["!", "!!", "!!!", "@", "#", "$", "*", ".", "?", "_", "-", "1!", "123!", "!1"]

PREFIXES = ["", "my", "the", "im", "iam", "mr", "ms", "xx", "x"]

LEET = str.maketrans({"a": "@", "e": "3", "i": "1", "o": "0", "s": "$", "t": "7"})


def build() -> set[str]:
    out: set[str] = set()

    out.update(LITERAL)
    out.update(BASE)

    for word in BASE:
        for suffix in DIGIT_SUFFIXES:
            out.add(word + suffix)
        for suffix in SYMBOL_SUFFIXES:
            out.add(word + suffix)
        for year in YEARS:
            out.add(word + year)
            out.add(word + year + "!")
        for year in SHORT_YEARS:
            out.add(word + year)
        # Leetspeak, and leetspeak carrying the two most common suffixes. Applied to the
        # base word only: the full cross-product would multiply the file size for entries
        # nobody types.
        leet = word.translate(LEET)
        if leet != word:
            out.update({leet, leet + "1", leet + "123", leet + "!", leet + "2024"})

    # Prefixed forms, for the shorter and most-used bases only — this is where the
    # cross-product would otherwise run away.
    for word in BASE[:400]:
        for prefix in PREFIXES:
            if not prefix:
                continue
            out.add(prefix + word)
            out.add(prefix + word + "123")
            out.add(prefix + word + "2024")

    # Long digit runs. These matter more than they look: the 12-character minimum pushes
    # people who want a short password towards simply typing further along the row, and
    # `1234567890123` is a very short walk from `123456`. Ascending and descending, from
    # every starting digit, out to well past the minimum length.
    ascending = "0123456789" * 4
    descending = "9876543210" * 4
    for start in range(10):
        for length in range(6, 33):
            out.add(ascending[start : start + length])
            out.add(descending[start : start + length])
    # Repeated short groups: 12341234..., 123123..., 1212...
    for group in ("12", "123", "1234", "12345", "123456", "0", "1", "69", "007", "1q2w3e"):
        for repeat in range(2, 9):
            out.add(group * repeat)

    # Repeated runs and simple keyboard walks with digits appended.
    for char in "abcdefghijklmnopqrstuvwxyz0123456789":
        for length in (6, 7, 8, 9, 10, 12):
            out.add(char * length)
    for walk in ("qwerty", "asdfgh", "zxcvbn", "qwertyuiop", "asdfghjkl", "zxcvbnm", "azerty"):
        for suffix in ("", "1", "12", "123", "1234", "12345", "123456", "!", "2024", "2025"):
            out.add(walk + suffix)

    return {entry.casefold() for entry in out if entry}


def main() -> int:
    entries = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Generated by tools/build_password_list.py — do not edit by hand.\n"
        "# NOT the real top-100k breach corpus; see the module docstring in\n"
        "# app/security/breached.py for exactly what this covers and what it does not.\n"
    )
    body = "\n".join(sorted(entries))
    # Written with an explicit newline so the file is byte-identical on Windows and Linux;
    # otherwise the entry count is the same and the checksum is not.
    OUT.write_text(header + body + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {len(entries):,} entries to {OUT} ({OUT.stat().st_size / 1024:.0f} KiB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
