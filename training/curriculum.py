"""1000+ generated training tasks for Helm, from easy to extreme.

Each task: id, level, say (what the user types), expect (tool it should use),
check(out, reply) -> bool, optional hint (shown on the retry), optional setup.
Checks look at the real Mac state (volume, clipboard, browser URL, app text)
or the tool output, never just at what the model claims.
"""

from __future__ import annotations

import re
import subprocess
from operator import add, mul, sub

from pathlib import Path

from grader import passes
from screen_elements import read_app

KNOWLEDGE = Path.home() / "Library/Application Support/Local Desktop Assistant/knowledge/apps.json"


def load_apps() -> list[dict]:
    import json

    try:
        return json.loads(KNOWLEDGE.read_text(encoding="utf-8"))["apps"]
    except (OSError, ValueError, KeyError):
        return []


def first_price(text: str) -> str | None:
    match = re.search(r"\$(\d+(?:\.\d\d)?)", text)
    return match.group(1) if match else None


def paid_price(app: dict) -> str | None:
    """First non-zero price in the app's plans (essays must cite a real paid price, not $0)."""
    for text in app["plans"].values():
        price = first_price(text)
        if price and float(price) > 0:
            return price
    return None


def paid_prices(app: dict) -> list[str]:
    return [p for p in (first_price(t) for t in app["plans"].values()) if p and float(p) > 0]


def essay_text() -> str:
    return read_app("TextEdit", max_chars=20000)[0]


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


def clear_calculator() -> None:
    """Start every Calculator task from 0."""
    from screen_elements import find_in_app, press

    subprocess.run(["open", "-a", "Calculator"], capture_output=True)
    for label in ("All Clear", "Clear"):
        element, _ = find_in_app("Calculator", lambda text, label=label: text == label)
        if element is not None:
            press(element)


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
    "Hello from Helm", "Training day one", "Jarvis is online", "The quick brown fox", "Systems nominal",
    "Good morning world", "Testing one two three", "Helm was here", "Learning every day", "Mission complete",
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
                     setup=clear_calculator)
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
             "ask_chatgpt", number_in(a * b),
             hint="ask_chatgpt with the question")
    for phrase in PHRASES * 2:
        n = sum(1 for t in tasks if t["id"].startswith(f"type-{phrase}"))
        task("hard", f"type-{phrase}-{n}", f"type {phrase} into TextEdit", "type_in_app",
             lambda out, reply, phrase=phrase: phrase in textedit_text(),
             hint="new_document with the text", setup=fresh_textedit)

    # ----- EXTREME ---------------------------------------------------------------
    for topic in TOPICS:
        task("extreme", f"chain-yt-textedit-{topic}",
             f"search youtube for {topic}, copy the first video link, and type it into a new TextEdit document",
             "web_search", lambda out, reply: "youtube.com/" in textedit_text(),
             hint="web_search youtube -> new_document with the first video link",
             setup=fresh_textedit)
    for country, capital in list(CAPITALS.items())[:30]:
        task("extreme", f"chain-chatgpt-textedit-{country}",
             f"ask ChatGPT the capital of {country}, then type its answer into a new TextEdit document",
             "ask_chatgpt", lambda out, reply, capital=capital: capital.lower() in textedit_text().lower(),
             hint="ask_chatgpt -> new_document with the answer",
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
             "send_message",
             # #polls is read-only for this account: it must notice; others may genuinely allow posting
             lambda out, reply, channel=channel: "permission" in out.lower() if channel == "polls"
             else ("permission" in out.lower() or "Dry run: opened" in out),
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
             "click_in_app", number_in(a * b), setup=clear_calculator,
             hint=f"click_in_app {' '.join(str(a))} Multiply {' '.join(str(b))} Equals one at a time, then read the display")
    for country, capital in list(CAPITALS.items())[:25]:
        task("insane", f"hop-{country}",
             f"find the capital of {country} on the web, then ask ChatGPT how many letters that city name has, "
             "and type ChatGPT's number into a new TextEdit document",
             "web_search",
             lambda out, reply, capital=capital: str(len(capital.replace(" ", ""))) in textedit_text(),
             hint="web_search -> ask_chatgpt -> new_document with the answer",
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
             hint="web_search youtube -> clipboard copy -> ask_chatgpt with the link and the request")
    # ----- WRITING: essays graded by macOS spelling + grammar, facts from private memory -----
    apps = load_apps()
    paid = [a for a in apps if paid_price(a)]
    for app in paid:
        for plan, price_text in app["plans"].items():
            price = first_price(price_text)
            if price:
                task("medium", f"price-{app['name']}-{plan}", f"how much does {app['name']} {plan} cost per month?",
                     None, lambda out, reply, price=price: price in reply.replace(",", ""),
                     hint="use your private notes or app_knowledge")
    for app in apps:
        task("hard", f"para-{app['name']}",
             f"write a 150-word paragraph explaining what {app['name']} is and how people use it, "
             "in a new TextEdit document, with perfect spelling and grammar",
             "new_document", lambda out, reply, name=app["name"]: passes(essay_text(), 120, 1, 1, [name]),
             hint="compose the whole paragraph yourself, then new_document(text=paragraph)", setup=fresh_textedit)
    for app in paid:
        price, prices = paid_price(app), paid_prices(app)
        task("extreme", f"essay-{app['name']}",
             f"write a 300-word essay about {app['name']} with an introduction, a body and a conclusion: how it "
             "works, how people use it, its benefits, and its subscription prices. Put it in a new TextEdit "
             "document with proper grammar.",
             "new_document", lambda out, reply, name=app["name"], prices=prices: passes(essay_text(), 250, 3, 2, [name, prices]),
             hint="check app_knowledge for prices, write 3+ paragraphs yourself, then new_document", setup=fresh_textedit)
    by_category: dict[str, list[dict]] = {}
    for app in paid:
        by_category.setdefault(app["category"], []).append(app)
    pairs = [(a, b) for group in by_category.values() for i, a in enumerate(group) for b in group[i + 1:]]
    for a, b in pairs:
        pa, pb = paid_prices(a), paid_prices(b)
        task("insane", f"compare-essay-{a['name']}-{b['name']}",
             f"write a 350-word comparison essay of {a['name']} versus {b['name']}: what each does, who should "
             "pick which, and what their plans cost. Use at least four paragraphs and flawless grammar, in a new "
             "TextEdit document.",
             "new_document",
             lambda out, reply, a=a["name"], b=b["name"], pa=pa, pb=pb: passes(essay_text(), 300, 4, 3, [a, b, pa, pb]),
             hint="app_knowledge for both apps, then write four paragraphs yourself and new_document", setup=fresh_textedit)
    monthly = []
    for app in paid:
        for plan, price_text in app["plans"].items():
            if ("/month" in price_text and first_price(price_text) and float(first_price(price_text)) > 0
                    and "year" not in price_text.split("/month")[0]):
                monthly.append((app["name"], plan, float(first_price(price_text))))
                break
    for i in range(min(30, max(0, len(monthly) - 2))):
        trio = [monthly[i], monthly[(i + 7) % len(monthly)], monthly[(i + 13) % len(monthly)]]
        if len({t[0] for t in trio}) < 3:
            continue
        total = f"{sum(t[2] for t in trio):.2f}"
        listed = ", ".join(f"{name} {plan}" for name, plan, _ in trio)
        task("master", f"budget-{i}",
             f"I pay for {listed}. Write a 400-word essay in a new TextEdit document that works out my exact total "
             "monthly cost, explains what each subscription gives me, and advises which one I could cancel. "
             "Use at least four paragraphs and proper grammar.",
             "new_document", lambda out, reply, total=total: passes(essay_text(), 330, 4, 3, [total]),
             hint="look up each price, add them carefully, state the total with cents, then write the essay",
             setup=fresh_textedit)
    for app in paid[:30]:
        price = paid_prices(app)
        task("master", f"persuade-{app['name']}",
             f"write a persuasive 400-word letter to a parent arguing whether a paid {app['name']} plan is worth "
             "the money, with a greeting, at least four paragraphs, a sign-off and flawless grammar, in a new "
             "TextEdit document",
             "new_document", lambda out, reply, name=app["name"], price=price: passes(essay_text(), 330, 4, 3, [name, price, "dear"]),
             hint="use the real price from app_knowledge; Dear ..., four paragraphs, then a sign-off", setup=fresh_textedit)
    return tasks


def _no_outsourcing(tasks: list[dict]) -> list[dict]:
    """Writing tasks fail if Helm had ChatGPT write the text instead of writing it itself."""
    for task in tasks:
        if task["expect"] == "new_document":
            task["check"] = (lambda out, reply, original=task["check"]:
                             "TOOL: ask_chatgpt" not in out and original(out, reply))
    return tasks


TASKS = _no_outsourcing(build())
LEVELS = ("easy", "medium", "hard", "extreme", "insane", "master")

if __name__ == "__main__":
    from collections import Counter

    print(len(TASKS), "tasks:", dict(Counter(t["level"] for t in TASKS)))
    assert len({t["id"] for t in TASKS}) == len(TASKS), "task ids must be unique"
