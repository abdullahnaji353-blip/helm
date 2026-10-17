"""1000+ generated training tasks for Chatbot, from easy to extreme.

Each task: id, level, say (what the user types), expect (tool it should use),
check(out, reply) -> bool, optional hint (shown on the retry), optional setup.
Checks look at the real Mac state (volume, clipboard, browser URL, app text)
or the tool output, never just at what the model claims.
"""

from __future__ import annotations

import re
import subprocess
from operator import add, mul, sub

from screen_elements import read_app


def sh(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True).stdout.strip()


def volume() -> str:
    return sh("osascript", "-e", "output volume of (get volume settings)")


def clipboard() -> str:
    return sh("pbpaste")


def safari_url() -> str:
    return sh("osascript", "-e", 'tell application "Safari" to get URL of front document')


def textedit_text() -> str:
    return read_app("TextEdit")[0]


def said_bot(reply: str) -> bool:
    """True if the reply calls them a bot (and not 'not a bot')."""
    text = reply.lower()
    return "bot" in text and not re.search(r"(not|isn't|is not|n't) (a )?bot|real person|human", text)


def battery() -> int:
    match = re.search(r"(\d+)%", sh("pmset", "-g", "batt"))
    return int(match.group(1)) if match else 100


def fresh_textedit() -> None:
    subprocess.run(["osascript", "-e", 'tell application "TextEdit" to close every document saving no'],
                   capture_output=True)


def quit_app(name: str):
    return lambda: subprocess.run(["osascript", "-e", f'tell application "{name}" to quit'], capture_output=True)


def number_in(value: int):
    def check(out, reply):
        text = reply.replace(",", "").replace("\u2212", "-").replace("\u200e", "")
        return re.search(rf"(?<![\d-]){value}(?!\d)", text) is not None
    return check


DM_NAMES = [
    "Tizi aziz", "FRØST flixxy7!", "Welcomer", "the boys", "Tickety", "owners", "STK", "Yunite",
    "ahmedhamid4551", "Yousefplays", "Daboiomar13", "Jxcob", "RVL Sky", ".yesyes26", "SMT | jstparkz",
]
SAFE_APPS = [
    "Calculator", "TextEdit", "Notes", "Calendar", "Clock", "Weather", "Stocks", "Maps", "Music",
    "Podcasts", "News", "Books", "Preview", "Reminders", "Freeform", "Stickies", "Dictionary", "Chess",
    "Font Book", "Photo Booth",
]
WORDS = [
    "banana", "rocket", "guitar", "planet", "falcon", "pixel", "thunder", "galaxy", "harbor", "mango",
    "violet", "lantern", "cactus", "dragon", "meadow", "quartz", "saturn", "tornado", "walnut", "zebra",
    "anchor", "breeze", "candle", "dolphin", "ember", "forest", "glacier", "hammer", "island", "jungle",
    "kettle", "lemon", "magnet", "nectar", "orbit", "pepper", "quiver", "river", "shadow", "tiger",
    "umbrella", "velvet", "window", "yonder", "zipper", "amber", "bronze", "cobalt", "denim", "eagle",
]
TOPICS = [
    "lofi music", "how to train an AI", "SwiftUI tutorial", "python for beginners", "cooking pasta",
    "latte art", "minecraft builds", "fortnite tips", "guitar chords", "drawing anime", "home workout",
    "space documentary", "car reviews", "skateboarding tricks", "video editing", "chess openings",
    "learn spanish", "street food", "cat videos", "dog training", "photography tips", "piano tutorial",
    "football highlights", "basketball skills", "AI tools", "ChatGPT tips", "how to code a website",
    "machine learning explained", "blender 3d", "davinci resolve tutorial",
]
SITES = [
    "wikipedia.org", "example.com", "apple.com", "github.com", "youtube.com", "reddit.com",
    "bbc.com", "nasa.gov", "weather.com", "imdb.com", "stackoverflow.com", "python.org",
    "mozilla.org", "archive.org", "w3schools.com", "khanacademy.org", "duolingo.com", "spotify.com",
    "discord.com", "twitch.tv",
]
CAPITALS = {
    "France": "Paris", "Japan": "Tokyo", "Italy": "Rome", "Spain": "Madrid", "Germany": "Berlin",
    "Egypt": "Cairo", "Canada": "Ottawa", "Australia": "Canberra", "Brazil": "Bras", "Kenya": "Nairobi",
    "Jordan": "Amman", "Turkey": "Ankara", "Norway": "Oslo", "Sweden": "Stockholm", "Greece": "Athens",
    "Portugal": "Lisbon", "Peru": "Lima", "Chile": "Santiago", "Mexico": "Mexico City", "India": "New Delhi",
    "China": "Beijing", "Russia": "Moscow", "Ireland": "Dublin", "Poland": "Warsaw", "Austria": "Vienna",
    "Thailand": "Bangkok", "Vietnam": "Hanoi", "Argentina": "Buenos Aires", "Morocco": "Rabat",
    "Saudi Arabia": "Riyadh", "Qatar": "Doha", "Iraq": "Baghdad", "Lebanon": "Beirut", "Cuba": "Havana",
    "Finland": "Helsinki", "Denmark": "Copenhagen", "Hungary": "Budapest", "Nigeria": "Abuja",
    "Colombia": "Bogot", "South Korea": "Seoul",
}
CURRENCIES = {
    "Japan": "yen", "United Kingdom": "pound", "India": "rupee", "China": "yuan", "Mexico": "peso",
    "Russia": "ruble", "Brazil": "real", "Jordan": "dinar", "Egypt": "pound", "Turkey": "lira",
    "Switzerland": "franc", "South Korea": "won", "Thailand": "baht", "Vietnam": "dong", "Poland": "zloty",
    "Sweden": "krona", "Norway": "krone", "Denmark": "krone", "Saudi Arabia": "riyal", "Qatar": "riyal",
    "United States": "dollar", "Canada": "dollar", "Australia": "dollar", "Germany": "euro", "France": "euro",
    "Italy": "euro", "Spain": "euro", "Nigeria": "naira", "Kenya": "shilling", "Israel": "shekel",
    "Indonesia": "rupiah", "Malaysia": "ringgit", "Philippines": "peso", "Argentina": "peso",
    "Chile": "peso", "Hungary": "forint", "Czech Republic": "koruna", "South Africa": "rand",
    "Iraq": "dinar", "Morocco": "dirham",
}
PHRASES = [
    "Hello from Chatbot", "Training day one", "Jarvis is online", "The quick brown fox", "Systems nominal",
    "Good morning world", "Testing one two three", "Chatbot was here", "Learning every day", "Mission complete",
]
STATUS_PHRASES = [
    "what's my battery and the time?", "what time is it", "how much battery do I have", "what's today's date",
    "is my mac charging", "tell me the time", "battery level please", "what day is it today",
    "give me a system status", "how's my battery doing", "what's the date and time", "check my battery",
    "time check", "what time is it right now", "status report", "do I need to charge my mac",
    "what's my charge level", "what's the current time", "tell me today's date", "battery?",
]
OPS = {"plus": ("Add", add, "+"), "minus": ("Subtract", sub, "-"), "times": ("Multiply", mul, "x")}


def build() -> list[dict]:
    tasks: list[dict] = []

    def task(level, tid, say, expect, check, hint=None, setup=None, cleanup=None):
        tasks.append({"id": tid, "level": level, "say": say, "expect": expect, "check": check,
                      "hint": hint, "setup": setup, "cleanup": cleanup})

    # ----- EASY ------------------------------------------------------------------
    for level in range(0, 101):
        task("easy", f"volume-{level}", f"set the volume to {level}", "set_volume",
             lambda out, reply, level=level: volume() == str(level))
    for word in WORDS * 2:
        n = sum(1 for t in tasks if t["id"].startswith(f"copy-{word}"))
        say = f"copy the word {word} to the clipboard" if n == 0 else f"put {word} on my clipboard"
        task("easy", f"copy-{word}-{n}", say, "clipboard",
             lambda out, reply, word=word: clipboard().strip().lower() == word)
    for a in range(2, 14):
        for b in range(3, 15):
            task("easy", f"math-{a}x{b}", f"what is {a} times {b}?", None, number_in(a * b))
    for a in range(11, 31):
        for b in range(5, 10):
            task("easy", f"math-{a}+{b * 7}", f"what is {a} plus {b * 7}?", None, number_in(a + b * 7))
    for country, capital in CAPITALS.items():
        task("easy", f"know-capital-{country}", f"what's the capital of {country}?", None,
             lambda out, reply, capital=capital: capital.lower() in reply.lower())
    for country, money in CURRENCIES.items():
        task("easy", f"know-currency-{country}", f"what currency does {country} use?", None,
             lambda out, reply, money=money: money in reply.lower())
    for i, phrase in enumerate(STATUS_PHRASES):
        task("easy", f"status-{i}", phrase, "system_status",
             lambda out, reply: bool(re.search(r"\d", reply)))
    for app in SAFE_APPS:
        for say in (f"open {app}", f"launch {app} please"):
            task("easy", f"open-{app}-{say.split()[0]}", say, "open_app",
                 lambda out, reply, app=app: bool(sh("pgrep", "-xi", app.replace(" ", ""))) or app in sh(
                     "osascript", "-e", 'tell application "System Events" to get name of every process'),
                 cleanup=quit_app(app))

    # ----- MEDIUM ----------------------------------------------------------------
    for word, (button, fn, _) in OPS.items():
        for a in range(1, 10):
            for b in range(1, 10):
                if (a + b) % 2:  # half the grid keeps it to ~120 tasks
                    continue
                task("medium", f"calc-{a}{word}{b}",
                     f"in Calculator press {a}, then {button.lower()}, then {b}, then equals, and tell me the result",
                     "click_in_app", number_in(fn(a, b)),
                     hint="click_in_app for each button, then read_screen Calculator",
                     setup=lambda: subprocess.run(["osascript", "-e", 'tell application "Calculator" to activate'],
                                                  capture_output=True))
    for topic in TOPICS:
        task("medium", f"yt-{topic}", f"find youtube videos about {topic}", "web_search",
             lambda out, reply: "youtube.com/watch" in out or "youtube.com/shorts" in out)
        task("medium", f"tt-{topic}", f"search tiktok for {topic}", "web_search",
             lambda out, reply: "tiktok.com" in out)
        task("medium", f"yt2-{topic}", f"show me some {topic} videos on youtube", "web_search",
             lambda out, reply: "youtube.com/watch" in out or "youtube.com/shorts" in out)
        task("medium", f"tt2-{topic}", f"find {topic} examples on tiktok", "web_search",
             lambda out, reply: "tiktok.com" in out)
    for name in DM_NAMES:
        short = name.split("|")[-1].strip()
        for say in (f"go to discord then {short} then say hi", f"dm {short} on discord saying yo"):
            task("medium", f"dm-{short}-{say.split()[0]}", say, "send_message",
                 lambda out, reply: "Dry run: opened and verified" in out,
                 hint=f"it's a direct message with a person named {name}")
    for site in SITES:
        for say in (f"go to {site} in Safari", f"open {site} in safari"):
            task("medium", f"safari-{site}-{say.split()[0]}", say, "keys_in_app",
                 lambda out, reply, site=site: site.split(".")[0] in safari_url(),
                 hint="keys_in_app command+l in Safari, then type_in_app with submit")
    for app in ("TextEdit", "Notes", "Stickies"):
        task("medium", f"menu-new-{app}", f"make a new {app} document using the File menu", "menu_in_app",
             lambda out, reply: "Chose File > New" in out, hint="menu_in_app with path File > New")

    # ----- HARD ------------------------------------------------------------------
    for country, capital in CAPITALS.items():
        task("hard", f"fact-capital-{country}", f"search the web for the capital of {country} and tell me",
             "web_search", lambda out, reply, capital=capital: capital.lower() in reply.lower(),
             hint="web_search, then read the results")
    for country, money in CURRENCIES.items():
        task("hard", f"fact-currency-{country}", f"look up on the web what currency {country} uses and tell me",
             "web_search", lambda out, reply, money=money: money in reply.lower(),
             hint="web_search, then read the results")
    for topic in TOPICS:
        task("hard", f"clip-{topic}", f"find a youtube video about {topic} and copy its link to the clipboard",
             "web_search", lambda out, reply: "youtube.com/" in clipboard(),
             hint="web_search on youtube, then clipboard copy of one link")
    for a, b in [(17, 3), (12, 12), (25, 4), (9, 8), (13, 7), (21, 6), (15, 15), (11, 9), (14, 5), (19, 2),
                 (23, 3), (16, 6), (18, 4), (27, 3), (32, 2), (45, 2), (8, 8), (7, 9), (6, 13), (5, 17)]:
        task("hard", f"chatgpt-{a}x{b}", f"ask ChatGPT what {a} times {b} is and tell me what it answered",
             "paste_into_app", number_in(a * b),
             hint="paste the question into ChatGPT with submit, wait, then read_screen ChatGPT")
    for phrase in PHRASES * 2:
        n = sum(1 for t in tasks if t["id"].startswith(f"type-{phrase}"))
        task("hard", f"type-{phrase}-{n}", f"type {phrase} into TextEdit", "type_in_app",
             lambda out, reply, phrase=phrase: phrase in textedit_text(),
             hint="menu_in_app File > New in TextEdit, then type_in_app", setup=fresh_textedit)

    # ----- EXTREME ---------------------------------------------------------------
    for topic in TOPICS:
        task("extreme", f"chain-yt-textedit-{topic}",
             f"search youtube for {topic}, copy the first video link, and type it into a new TextEdit document",
             "web_search", lambda out, reply: "youtube.com/" in textedit_text(),
             hint="web_search youtube -> clipboard copy -> menu_in_app TextEdit File > New -> type_in_app",
             setup=fresh_textedit)
    for country, capital in list(CAPITALS.items())[:30]:
        task("extreme", f"chain-chatgpt-textedit-{country}",
             f"ask ChatGPT the capital of {country}, then type its answer into a new TextEdit document",
             "paste_into_app", lambda out, reply, capital=capital: capital.lower() in textedit_text().lower(),
             hint="paste_into_app ChatGPT submit -> read_screen ChatGPT -> TextEdit File > New -> type_in_app",
             setup=fresh_textedit)
    for word in WORDS[:30]:
        task("extreme", f"chain-copy-paste-{word}",
             f"copy the word {word}, then paste it into a new TextEdit document",
             "clipboard", lambda out, reply, word=word: word in textedit_text().lower(),
             hint="clipboard copy -> menu_in_app TextEdit File > New -> paste_into_app TextEdit",
             setup=fresh_textedit)
    for site in SITES:
        task("extreme", f"screen-{site}", f"use the screen to open Safari and go to {site}", "control_screen",
             lambda out, reply, site=site: site.split(".")[0] in safari_url(),
             hint="control_screen: open Safari, command+l, type the address, enter")
    for topic in TOPICS[:20]:
        task("extreme", f"research-{topic}",
             f"search the web for {topic}, read the top results and save one lesson you learned",
             "web_search", lambda out, reply: "TOOL: save_lesson" in out,
             hint="web_search -> read the results -> save_lesson")
    # ----- INSANE: decisions, traps, multi-app chains, verification ------------------
    bots, people = ["Welcomer", "Tickety", "Yunite"], ["Tizi aziz", "ahmedhamid4551", "Yousefplays", "Daboiomar13", "Jxcob"]
    for name in bots + people:
        expected_bot = name in bots
        task("insane", f"botcheck-{name}", f"on discord, check whether {name} is a bot or a real person and tell me",
             "send_message",
             lambda out, reply, expected_bot=expected_bot: said_bot(reply) == expected_bot,
             hint="open their DM (dry run): a person can be called, a bot has no call button")
    for channel in ("polls", "announcements", "rules", "events", "assets"):
        task("insane", f"noperm-{channel}", f"post hello in the {channel} channel of the KeyMau Esports server on discord",
             "send_message", lambda out, reply: "permission" in out.lower() or "Dry run: opened" in out,
             hint="it is a channel in a server; if posting is not allowed, say so")
    for threshold, low, high in [(50, 20, 40), (30, 15, 35), (80, 25, 45), (60, 10, 30), (20, 5, 50)]:
        task("insane", f"if-battery-{threshold}",
             f"if my battery is below {threshold}% set the volume to {low}, otherwise set it to {high}",
             "system_status",
             lambda out, reply, threshold=threshold, low=low, high=high:
             volume() == str(low if battery() < threshold else high),
             hint="system_status first, decide, then set_volume")
    for fake in ("Zorblax", "Quantum Toaster", "Blorp Studio", "Fakeapp Pro", "Nonexistent Notes"):
        task("insane", f"trap-open-{fake}", f"open the app {fake}", "open_app",
             lambda out, reply: any(w in reply.lower() for w in ("not installed", "couldn", "could not", "can't", "cannot", "not find", "doesn")),
             hint="if the app is not installed, say so honestly")
    for a, b in [(47, 23), (85, 12), (36, 19), (64, 27), (99, 11), (58, 34), (73, 16), (42, 42), (91, 28), (66, 15)]:
        task("insane", f"calc-big-{a}x{b}", f"use the Calculator buttons to work out {a} times {b} and tell me the answer",
             "click_in_app", number_in(a * b),
             hint=f"click_in_app {' '.join(str(a))} Multiply {' '.join(str(b))} Equals one at a time, then read the display")
    for country, capital in list(CAPITALS.items())[:25]:
        task("insane", f"hop-{country}",
             f"find the capital of {country} on the web, then ask ChatGPT how many letters that city name has, "
             "and type ChatGPT's number into a new TextEdit document",
             "web_search",
             lambda out, reply, capital=capital: str(len(capital.replace(" ", ""))) in textedit_text(),
             hint="web_search -> paste_into_app ChatGPT submit -> read_screen ChatGPT -> TextEdit File > New -> type_in_app",
             setup=fresh_textedit)
    for first, second, taller in [("Eiffel Tower", "Statue of Liberty", "eiffel"), ("Burj Khalifa", "Empire State Building", "burj"),
                                  ("Mount Everest", "K2", "everest"), ("Big Ben", "Leaning Tower of Pisa", "big ben"),
                                  ("CN Tower", "Space Needle", "cn tower")]:
        task("insane", f"compare-{first}", f"search the web for how tall the {first} and the {second} are and tell me which is taller",
             "web_search", lambda out, reply, taller=taller: taller in reply.lower().split("taller")[0] or reply.lower().startswith(taller),
             hint="search both heights, compare the numbers")
    for topic in TOPICS:
        task("insane", f"top3-{topic}", f"search youtube for {topic} and give me the titles of the top 3 videos as a numbered list",
             "web_search", lambda out, reply: all(f"{n}." in reply for n in (1, 2, 3)),
             hint="web_search youtube, then list three result titles as 1. 2. 3.")
    for word in WORDS[:20]:
        task("insane", f"clip-count-{word}", f"copy the word {word}, then read the clipboard back and tell me how many letters it has",
             "clipboard", lambda out, reply, word=word: number_in(len(word))(out, reply) and "TOOL: clipboard" in out,
             hint="clipboard copy, clipboard read, then count")
    for topic in TOPICS[:15]:
        task("insane", f"yt-to-chatgpt-{topic}",
             f"find a youtube video about {topic}, copy its link, then paste it into ChatGPT asking for a one line summary of what that video is probably about",
             "web_search", lambda out, reply: "youtube.com/" in clipboard() and "TOOL: paste_into_app" in out,
             hint="web_search youtube -> clipboard copy -> paste_into_app ChatGPT with the request and submit")
    return tasks


TASKS = build()
LEVELS = ("easy", "medium", "hard", "extreme", "insane")

if __name__ == "__main__":
    from collections import Counter

    print(len(TASKS), "tasks:", dict(Counter(t["level"] for t in TASKS)))
    assert len({t["id"] for t in TASKS}) == len(TASKS), "task ids must be unique"
