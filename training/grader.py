"""Grade Chatbot's writing with macOS's own spelling and grammar checker (NSSpellChecker)."""

from __future__ import annotations

import re


def _checker():
    from AppKit import NSSpellChecker

    return NSSpellChecker.sharedSpellChecker()


def spelling_errors(text: str, language: str = "en") -> list[str]:
    checker, start, found = _checker(), 0, []
    while start < len(text):
        found_range, _ = checker.checkSpellingOfString_startingAt_language_wrap_inSpellDocumentWithTag_wordCount_(
            text, start, language, False, 0, None)
        if found_range.length == 0:
            break
        found.append(text[found_range.location:found_range.location + found_range.length])
        start = found_range.location + found_range.length
    return found


def grammar_issues(text: str, language: str = "en") -> list[str]:
    checker, start, found = _checker(), 0, []
    while start < len(text):
        found_range, _ = checker.checkGrammarOfString_startingAt_language_wrap_inSpellDocumentWithTag_details_(
            text, start, language, False, 0, None)
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


def passes(text: str, min_words: int, min_paragraphs: int, max_errors: int, must_mention: list[str] = ()) -> bool:
    report = grade(text)
    lowered = text.lower().replace(",", "")
    return (report["words"] >= min_words and report["paragraphs"] >= min_paragraphs
            and len(report["spelling"]) + len(report["grammar"]) <= max_errors
            and all(term.lower() in lowered for term in must_mention))
