"""Grade Chatbot's writing with macOS's own spelling and grammar checker (NSSpellChecker)."""

from __future__ import annotations

import re


BRANDS = {"Canva", "CapCut", "Spotify", "Netflix", "Discord", "Duolingo", "iCloud", "ChatGPT", "TikTok", "Gemini",
          "Instagram", "WhatsApp", "Snapchat", "Telegram", "Notion", "Slack", "Zoom", "YouTube", "Xbox", "Steam",
          "DaVinci", "OBS", "macOS", "iOS", "Nitro", "OpenAI", "Anthropic", "Claude", "TextEdit", "iMessage"}
_TAG = None


def _checker():
    from AppKit import NSSpellChecker

    return NSSpellChecker.sharedSpellChecker()


def _tag() -> int:
    """A private spell document that ignores brand names (the user's dictionary is untouched)."""
    global _TAG
    if _TAG is None:
        from AppKit import NSSpellChecker

        checker = _checker()
        _TAG = NSSpellChecker.uniqueSpellDocumentTag()
        for word in BRANDS:
            checker.ignoreWord_inSpellDocumentWithTag_(word, _TAG)
    return _TAG


def spelling_errors(text: str, language: str = "en") -> list[str]:
    checker, start, found = _checker(), 0, []
    while start < len(text):
        found_range, _ = checker.checkSpellingOfString_startingAt_language_wrap_inSpellDocumentWithTag_wordCount_(
            text, start, language, False, _tag(), None)
        if found_range.length == 0:
            break
        found.append(text[found_range.location:found_range.location + found_range.length])
        start = found_range.location + found_range.length
    return found


def grammar_issues(text: str, language: str = "en") -> list[str]:
    checker, start, found = _checker(), 0, []
    while start < len(text):
        found_range, _ = checker.checkGrammarOfString_startingAt_language_wrap_inSpellDocumentWithTag_details_(
            text, start, language, False, _tag(), None)
        if found_range.length == 0:
            break
        found.append(text[found_range.location:found_range.location + found_range.length][:80])
        start = found_range.location + found_range.length
    return found


def grade(text: str) -> dict:
    words = re.findall(r"[A-Za-z0-9$'.%-]+", text)
    paragraphs = [p for p in re.split(r"\n\s*\n|\n", text) if len(p.split()) >= 15]
    return {
        "words": len(words),
        "paragraphs": len(paragraphs),
        "sentences": len(re.findall(r"[.!?](\s|$)", text)),
        "spelling": spelling_errors(text),
        "grammar": grammar_issues(text),
    }


def passes(text: str, min_words: int, min_paragraphs: int, max_errors: int, must_mention: list = ()) -> bool:
    """must_mention items are strings, or lists meaning "any one of these"."""
    report = grade(text)
    lowered = text.lower().replace(",", "")
    mentioned = all(
        any(option.lower() in lowered for option in (term if isinstance(term, (list, tuple)) else [term]))
        for term in must_mention
    )
    return (report["words"] >= min_words and report["paragraphs"] >= min_paragraphs
            and len(report["spelling"]) + len(report["grammar"]) <= max_errors and mentioned)
