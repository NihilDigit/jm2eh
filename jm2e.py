"""
JM2E: JMComic to E-Hentai link converter with fallback chain.

Query flow (4 queries max):
1. E-Hentai: Pure romaji (pykakasi)
2. E-Hentai: Romaji + English keywords (Jamdict dictionary lookup)
3. E-Hentai: English translation (SimplyTranslate API)
4. wnacg: Chinese title direct search
"""

import re
import urllib.parse
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

import httpx
import opencc_purepy as opencc
import pykakasi
import jmcomic
from ehentai import get_search
from jamdict import Jamdict

# Similarity threshold for matching
SIMILARITY_THRESHOLD = 0.55

# Initialize converters
_s2t = opencc.OpenCC("s2t")
_t2jp = opencc.OpenCC("t2jp")
_kks = pykakasi.kakasi()
_jam = Jamdict()

# Extra character mappings not handled by OpenCC
EXTRA_CHAR_MAP = {
    "糹": "糸",
}

# Extra character mappings for romaji conversion only (異体字 -> 正字 for correct reading)
ROMAJI_CHAR_MAP = {
    "娵": "嫁",  # 異体字: 華娵 -> 華嫁 (hanayome)
}

# SimplyTranslate API endpoint
TRANSLATE_API = "https://simplytranslate.org/api/translate"

# E-Hentai special characters that break search
EH_SPECIAL_CHARS = re.compile(r"[~\[\]{}()|\\^$*+?.]")


def clean_for_eh_search(text: str) -> str:
    """Remove special characters that break E-Hentai search."""
    return EH_SPECIAL_CHARS.sub(" ", text).strip()


def to_jp_kanji(text: str) -> str:
    """Convert Simplified Chinese to Japanese kanji via Traditional Chinese."""
    text = _t2jp.convert(_s2t.convert(text))
    for trad, jp in EXTRA_CHAR_MAP.items():
        text = text.replace(trad, jp)
    return text


def to_romaji(text: str) -> str:
    """Convert Japanese/Chinese text to pure romaji (no spaces)."""
    text = to_jp_kanji(text)
    # Apply romaji-specific character mappings for correct readings
    for orig, repl in ROMAJI_CHAR_MAP.items():
        text = text.replace(orig, repl)
    # Remove special characters
    text = re.sub(r"[①②③④⑤⑥⑦⑧⑨⑩♡♥☆★◆◇○●～〜]", "", text)
    result = _kks.convert(text)
    return "".join([item["hepburn"] for item in result]).lower().replace(" ", "")


def to_romaji_spaced(text: str) -> str:
    """Convert Japanese/Chinese text to romaji with spaces between words."""
    text = to_jp_kanji(text)
    # Apply romaji-specific character mappings for correct readings
    for orig, repl in ROMAJI_CHAR_MAP.items():
        text = text.replace(orig, repl)
    # Remove special characters
    text = re.sub(r"[①②③④⑤⑥⑦⑧⑨⑩♡♥☆★◆◇○●～〜]", " ", text)
    result = _kks.convert(text)
    parts = [item["hepburn"] for item in result if item["hepburn"]]
    return " ".join(parts).lower()


def lookup_english_keywords(text: str) -> list[str]:
    """Use Jamdict to find English translations for Japanese words.

    Returns a list of English keywords found in the dictionary.
    """
    text = to_jp_kanji(text)
    # Remove special characters and split into potential words
    text = re.sub(r"[①②③④⑤⑥⑦⑧⑨⑩♡♥☆★◆◇○●～〜\+\-\[\]\(\)]", " ", text)

    english_keywords = []

    # Try to look up each segment
    segments = _kks.convert(text)
    for seg in segments:
        orig = seg.get("orig", "")
        if not orig or len(orig) < 2:
            continue

        # Skip if already English/romaji
        if orig.isascii():
            english_keywords.append(orig.lower())
            continue

        # Look up in Jamdict
        try:
            result = _jam.lookup(orig)
            if result.entries:
                # Get the first sense's English gloss
                for entry in result.entries[:1]:
                    for sense in entry.senses[:1]:
                        for gloss in sense.gloss[:1]:
                            eng = str(gloss).lower()
                            # Filter out very common/generic words
                            if len(eng) >= 3 and eng not in [
                                "the",
                                "a",
                                "an",
                                "to",
                                "of",
                                "and",
                                "is",
                                "are",
                            ]:
                                english_keywords.append(eng)
        except Exception:
            pass

    return english_keywords[:5]  # Limit to 5 keywords


def _is_katakana_word(text: str) -> bool:
    """Check if text is primarily katakana (80%+ katakana characters)."""
    if not text:
        return False
    import unicodedata

    katakana_count = sum(1 for c in text if "KATAKANA" in unicodedata.name(c, ""))
    return katakana_count >= len(text) * 0.8


def _katakana_to_english(word: str) -> Optional[str]:
    """Convert katakana word to English using Jamdict dictionary.

    Only returns single English words (no phrases).
    """
    try:
        result = _jam.lookup(word)
        if result.entries:
            for entry in result.entries[:2]:
                for sense in entry.senses[:2]:
                    for gloss in sense.gloss[:2]:
                        eng = str(gloss).lower()
                        # Only single words, no phrases
                        if " " not in eng and len(eng) >= 3:
                            return eng
    except Exception:
        pass
    return None


def to_romaji_with_english(text: str) -> str:
    """Convert Japanese text to romaji, replacing ONLY katakana with English.

    Katakana words are typically loanwords from English, so converting them
    back to English often yields better search results.

    Example: '異邦ノ乙女シリーズ' -> 'ihou no otome series'
    (シリーズ is katakana -> series, 乙女 is kanji -> stays as romaji 'otome')
    """
    text = to_jp_kanji(text)
    # Remove special characters
    text = re.sub(r"[①②③④⑤⑥⑦⑧⑨⑩♡♥☆★◆◇○●～〜]", " ", text)

    segments = _kks.convert(text)
    result_parts = []

    for seg in segments:
        orig = seg.get("orig", "")
        hepburn = seg.get("hepburn", "")

        if not orig:
            continue

        # If already English/ASCII, keep it
        if orig.isascii():
            result_parts.append(orig.lower())
            continue

        # Only convert katakana words to English
        if _is_katakana_word(orig) and len(orig) >= 2:
            english_word = _katakana_to_english(orig)
            if english_word:
                result_parts.append(english_word)
                continue

        # Otherwise use romaji
        if hepburn:
            result_parts.append(hepburn)

    return " ".join(result_parts).lower()


def translate_to_english(text: str, source: str = "ja") -> Optional[str]:
    """Translate Japanese/Chinese text to English using SimplyTranslate API."""
    try:
        resp = httpx.get(
            TRANSLATE_API,
            params={"engine": "google", "from": source, "to": "en", "text": text},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("translated_text")
    except Exception as e:
        print(f"  Translation error: {e}")
    return None


def normalize_cjk(text: str) -> str:
    """Normalize for CJK comparison."""
    text = to_jp_kanji(text)
    return re.sub(r"[^a-z0-9\u3040-\u9faf]", "", text.lower())


def normalize_romaji(text: str) -> str:
    """Normalize for romaji comparison."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def extract_eh_title_parts(eh_title: str) -> tuple[str, list[str]]:
    """Extract parts from E-Hentai title.

    '[Author] Romaji Title | 中文标题 [Chinese]' -> ('Romaji Title', ['中文标题'])
    """
    # Remove [xxx] and (xxx) tags
    clean = re.sub(r"\[[^\]]+\]", "", eh_title)
    clean = re.sub(r"\([^\)]+\)", "", clean)
    # Split by |
    parts = [p.strip() for p in clean.split("|") if p.strip()]

    romaji_part = parts[0] if parts else ""
    other_parts = parts[1:] if len(parts) > 1 else []
    return romaji_part, other_parts


def calc_match_score(
    jm_oname: str, eh_title: str, jm_english: Optional[str] = None
) -> tuple[float, str]:
    """Calculate best match score between JM oname and EH title.

    Args:
        jm_oname: JM original name (Japanese/Chinese)
        eh_title: E-Hentai gallery title
        jm_english: Optional English translation of jm_oname

    Returns: (score, method)
    """
    romaji_part, other_parts = extract_eh_title_parts(eh_title)

    scores = []

    # Strategy 1: Direct match oname vs Chinese/Japanese parts
    jm_norm = normalize_cjk(jm_oname)
    for part in other_parts:
        part_norm = normalize_cjk(part)
        if part_norm and jm_norm:
            sim = SequenceMatcher(None, jm_norm, part_norm).ratio()
            scores.append((sim, "direct"))

    # Strategy 2: Romaji match
    if romaji_part:
        jm_romaji = to_romaji(jm_oname)
        romaji_norm = normalize_romaji(romaji_part)
        if jm_romaji and romaji_norm:
            sim = SequenceMatcher(None, jm_romaji, romaji_norm).ratio()
            scores.append((sim, "romaji"))
            # Strategy 2b: Check if JM romaji is a prefix of EH romaji
            # This handles cases like "Title + Title After Story" where JM only has "Title"
            if len(jm_romaji) >= 10 and romaji_norm.startswith(jm_romaji):
                scores.append((0.90, "romaji_prefix"))

    # Strategy 3: English translation match
    if jm_english and romaji_part:
        en_norm = normalize_romaji(jm_english)
        romaji_norm = normalize_romaji(romaji_part)
        if en_norm and romaji_norm:
            sim = SequenceMatcher(None, en_norm, romaji_norm).ratio()
            scores.append((sim, "english"))

    # Strategy 4: Check if jm_oname appears in EH title (substring/contains match)
    # Only use this for longer titles to avoid false positives with common words
    eh_title_lower = eh_title.lower()
    jm_oname_lower = jm_oname.lower()
    # Require at least 8 chars for contains match to avoid false positives like "SUMMER"
    if len(jm_oname_lower) >= 8 and jm_oname_lower in eh_title_lower:
        scores.append((0.85, "contains"))

    # Strategy 5: Check if jm_english appears in EH title
    # Same stricter requirement for English
    if jm_english and len(jm_english) >= 8:
        jm_english_lower = jm_english.lower()
        if jm_english_lower in eh_title_lower:
            scores.append((0.85, "contains_en"))

    if not scores:
        return 0.0, "none"

    best = max(scores, key=lambda x: x[0])
    return best[0], best[1]


@dataclass
class ConversionResult:
    """Result of JMComic ID to link conversion."""

    jm_id: str
    title: str
    author: str
    link: str
    source: str  # 'ehentai', 'wnacg', 'none'
    similarity: float = 0.0

    def __str__(self):
        return f"[{self.source.upper()}] {self.link}"


class JM2EConverter:
    """Converts JMComic IDs to E-Hentai links with fallback."""

    def __init__(self):
        self.jm_option = jmcomic.JmOption.default()
        self.jm_client = self.jm_option.new_jm_client()

    def get_jm_info(self, jm_id: str) -> dict:
        """Get album info from JMComic ID."""
        album = self.jm_client.get_album_detail(jm_id)
        oname = getattr(album, "oname", "") or album.title

        # Collect candidate titles for matching
        candidates = [oname]

        # Try to extract title from description (often has English/romaji title)
        description = getattr(album, "description", "") or ""
        if description:
            desc_title = self._extract_title_from_description(description)
            if desc_title and desc_title not in candidates:
                candidates.append(desc_title)

        # Try to extract English/romaji title from full title
        english_from_title = self._extract_english_from_title(album.title)
        if english_from_title and english_from_title not in candidates:
            candidates.append(english_from_title)

        return {
            "title": album.title,
            "author": album.author,
            "oname": oname,
            "candidates": candidates,
            "description": description,
        }

    def _extract_english_from_title(self, title: str) -> Optional[str]:
        """Extract English/romaji title from JM full title."""
        if not title:
            return None

        # Find the last ) and look for English text after it
        last_paren = title.rfind(")")
        if last_paren > 0 and last_paren < len(title) - 3:
            after_paren = title[last_paren + 1 :].strip()
            if after_paren and re.search(r"[a-zA-Z]{4,}", after_paren):
                after_paren = re.sub(r"^\s*[\[\]]+\s*", "", after_paren)
                after_paren = re.sub(r"\s*[\[\]]+\s*$", "", after_paren)
                if after_paren and len(after_paren) >= 4:
                    return after_paren
        return None

    def _extract_title_from_description(self, description: str) -> Optional[str]:
        """Extract title from JM description field."""
        if not description:
            return None

        # Remove common tags at the end
        clean = re.sub(r"\s*\[[^\]]*\]\s*$", "", description)
        clean = re.sub(r"\s*\[[^\]]*\]\s*$", "", clean)
        clean = re.sub(r"\s*\[[^\]]*\]\s*$", "", clean)

        # Try to extract title after [Author] or (Circle)
        for pattern in [r"\]\s*(.+)$", r"\)\s*(.+)$"]:
            match = re.search(pattern, clean)
            if match:
                title = match.group(1).strip()
                if title and len(title) >= 3:
                    return title

        if "[" not in clean and "(" not in clean:
            return clean.strip() if len(clean.strip()) >= 3 else None
        return None

    def search_ehentai_single(
        self, query: str, candidates: list[str], english_title: Optional[str] = None
    ) -> tuple[Optional[str], float]:
        """Single E-Hentai search query.

        Returns: (url, similarity_score) or (None, 0)
        """
        try:
            print(f"  [E-H] Searching: {query}")
            page = get_search(query, direct=True)

            best_match_url: Optional[str] = None
            best_match_name: Optional[str] = None
            best_total_score = 0.0

            for gallery in page.gl_table:
                gallery_name = gallery.name or ""
                if not gallery_name:
                    continue

                # Calculate score for each candidate and sum them
                # This way, a result that matches ALL candidates well ranks higher
                total_score = 0.0
                max_single_score = 0.0
                for candidate in candidates:
                    score, _ = calc_match_score(candidate, gallery_name, english_title)
                    total_score += score
                    max_single_score = max(max_single_score, score)

                # Use weighted score: prioritize high max score but also reward matching all candidates
                # This helps when one candidate is more specific (like English title with "Zenpen")
                weighted_score = (
                    max_single_score * 0.7 + (total_score / len(candidates)) * 0.3
                )

                if weighted_score > best_total_score:
                    best_total_score = weighted_score
                    best_match_url = gallery.view_url
                    best_match_name = gallery_name

            # Determine threshold - lower it if query is very specific (has CJK) and only one result
            threshold = SIMILARITY_THRESHOLD
            has_cjk_query = any("\u3040" <= c <= "\u9fff" for c in query)
            single_result = len(page.gl_table) == 1

            if has_cjk_query and single_result and best_total_score >= 0.15:
                # Very specific query with single result - trust it
                print(
                    f"  [E-H] ✓ Single result (CJK query): {best_total_score:.2f} | {best_match_name[:60] if best_match_name else ''}"
                )
                return best_match_url, max(best_total_score, 0.70)

            if best_match_url and best_total_score >= threshold and best_match_name:
                print(
                    f"  [E-H] ✓ Best: {best_total_score:.2f} | {best_match_name[:60]}"
                )
                return best_match_url, best_total_score

            if best_match_name:
                print(
                    f"  [E-H] ✗ Below threshold: {best_total_score:.2f} | {best_match_name[:60]}"
                )

        except Exception as e:
            print(f"  [E-H] Search error: {e}")

        return None, 0.0

    def _extract_jp_title(self, full_title: str) -> Optional[str]:
        """Extract Japanese title from JM full title.

        Looks for text containing hiragana/katakana after ] bracket.
        """
        # Common patterns for Japanese titles in JM format
        # Try to find text with Japanese kana after a ] bracket
        patterns = [
            r"\]\s*([^\[]*[\u3040-\u309F\u30A0-\u30FF][^\[]*)",  # After ]
            r"([\u3040-\u309F\u30A0-\u30FF][\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\u0020-\u007E～〜]+)",  # Kana-heavy segment
        ]

        for pattern in patterns:
            match = re.search(pattern, full_title)
            if match:
                jp_part = match.group(1).strip()
                # Verify it has enough kana (at least 3)
                kana_count = sum(1 for c in jp_part if "\u3040" <= c <= "\u30ff")
                if kana_count >= 3:
                    # Clean up: remove trailing tags like [中国翻译]
                    jp_part = re.sub(r"\s*\[[^\]]*\]\s*$", "", jp_part).strip()
                    return jp_part
        return None

    def search_wnacg(
        self, oname: str, candidates: list[str], full_title: str = "", author: str = ""
    ) -> tuple[Optional[str], float]:
        """Search wnacg.com for Chinese versions."""
        from bs4 import BeautifulSoup
        from curl_cffi import requests as curl_requests

        best_match_url: Optional[str] = None
        best_match_title: Optional[str] = None
        best_score = 0.0

        # Build search queries: try Japanese title first, then Chinese oname
        search_queries: list[tuple[str, bool]] = []  # (query, is_japanese)

        # Try to extract Japanese title from full title and convert to proper kanji
        if full_title:
            jp_title = self._extract_jp_title(full_title)
            if jp_title:
                # Convert simplified Chinese chars to Japanese kanji
                jp_title_converted = to_jp_kanji(jp_title)
                # Take first part (before any series markers like 2～)
                jp_search = re.sub(r"[\d]+[～〜].*", "", jp_title_converted).strip()
                if len(jp_search) >= 4:
                    search_queries.append((jp_search, True))

        # Also try Chinese oname
        clean_oname = re.sub(r"[\d\s\+]+$", "", oname).strip()
        if clean_oname and len(clean_oname) >= 3:
            search_queries.append((clean_oname, False))

        if not search_queries:
            return None, 0.0

        # Prepare author name variations for matching
        author_jp = to_jp_kanji(author) if author else ""

        for search_term, is_japanese in search_queries:
            try:
                encoded_query = urllib.parse.quote(search_term)
                url = f"https://www.wnacg.com/search/?q={encoded_query}&f=_all&s=create_time_DESC&syn=yes"
                print(f"  [wnacg] Searching: {search_term[:50]}")

                # Use curl_cffi to bypass wnacg's httpx blocking
                resp = curl_requests.get(url, impersonate="chrome", timeout=15)
                if resp.status_code != 200:
                    continue

                soup = BeautifulSoup(resp.text, "lxml")
                seen_urls: set[str] = set()  # Deduplicate results

                for link in soup.find_all("a", href=True):
                    href = str(link.get("href", ""))
                    if "/photos-index-aid-" not in href:
                        continue

                    # Deduplicate
                    if href in seen_urls:
                        continue
                    seen_urls.add(href)

                    # Get title from link title attribute or text
                    title = str(link.get("title", "")) or link.get_text(strip=True)
                    # Remove HTML tags that might be in title attribute
                    title = re.sub(r"<[^>]+>", "", title)
                    if not title:
                        continue

                    # Only match Chinese versions
                    if not re.search(r"中[国國]翻[译譯]|[汉漢]化|中文", title):
                        continue

                    gallery_url = (
                        f"https://www.wnacg.com{href}" if href.startswith("/") else href
                    )

                    # For Japanese title search: verify author name to avoid false positives
                    # Common titles like "かわいい" can match many unrelated works
                    if is_japanese:
                        # Check if author name appears in the wnacg title
                        if author and (author in title or author_jp in title):
                            print(f"  [wnacg] ✓ JP+Author match: {title[:60]}")
                            return gallery_url, 0.90
                        # If no author match, continue searching (don't return immediately)
                        continue

                    # For Chinese oname search: try candidate matching
                    for candidate in candidates:
                        clean_candidate = re.sub(r"[\d\s\+]+$", "", candidate).strip()
                        if not clean_candidate or len(clean_candidate) < 3:
                            continue

                        # Contains match - for wnacg we're more lenient since we already
                        # filter by Chinese translation tags. Require at least 4 chars.
                        if len(clean_candidate) >= 4 and clean_candidate in title:
                            print(f"  [wnacg] ✓ Contains: {title[:60]}")
                            return gallery_url, 0.90

                        # Similarity match
                        jm_norm = normalize_cjk(candidate)
                        title_norm = normalize_cjk(title)
                        if jm_norm and title_norm:
                            sim = SequenceMatcher(None, jm_norm, title_norm).ratio()
                            if sim > best_score:
                                best_score = sim
                                best_match_url = gallery_url
                                best_match_title = title

                            if sim >= SIMILARITY_THRESHOLD:
                                print(f"  [wnacg] ✓ Match: {sim:.2f} | {title[:60]}")
                                return gallery_url, sim

            except Exception as e:
                print(f"  [wnacg] Search error: {e}")

        if best_match_url and best_score >= SIMILARITY_THRESHOLD and best_match_title:
            print(f"  [wnacg] ✓ Best: {best_score:.2f} | {best_match_title[:60]}")
            return best_match_url, best_score

        return None, best_score

    def convert(self, jm_id: str) -> ConversionResult:
        """Convert JMComic ID to link with 4-query flow.

        Query flow:
        1. E-Hentai: Best available title (English from desc/title, or romaji)
        2. E-Hentai: Romaji + English keywords (Jamdict)
        3. E-Hentai: English translation (SimplyTranslate)
        4. wnacg: Chinese title search
        """
        info = self.get_jm_info(jm_id)
        title = info["title"]
        author = info["author"]
        oname = info["oname"]
        candidates = info["candidates"]
        description = info.get("description", "")

        print(f"JM{jm_id}: {title}")

        # Prepare author romaji
        author_romaji = to_romaji(author) if author else ""

        # Check for existing English title in description/title
        english_from_desc = self._extract_title_from_description(description)
        has_english_desc = english_from_desc and re.search(
            r"[a-zA-Z]{3,}", english_from_desc
        )

        # Also check for English appended at end of title
        english_from_title = self._extract_english_from_title(title)
        has_english_title = english_from_title and re.search(
            r"[a-zA-Z]{3,}", english_from_title
        )

        # Best English title
        english_title = english_from_desc if has_english_desc else english_from_title

        if english_title:
            candidates.append(english_title)

        # --- Query 1: Best available title ---
        # If we have English, use it; otherwise use romaji
        if english_title:
            title_for_search = clean_for_eh_search(english_title)
        else:
            title_for_search = to_romaji_spaced(oname)

        # Truncate to first 4 words
        words = title_for_search.split()
        if len(words) > 4:
            title_for_search = " ".join(words[:4])

        query1 = f"{author_romaji} {title_for_search} l:chinese".strip()
        link, sim = self.search_ehentai_single(query1, candidates, english_title)
        if link:
            return ConversionResult(
                jm_id=jm_id,
                title=title,
                author=author,
                link=link,
                source="ehentai",
                similarity=sim,
            )

        # --- Query 1b: Quoted title without author (fallback for bad author romaji) ---
        # This helps when author name romaji is wrong (e.g., 柳田史太 -> yanagidashita vs yanagida fumita)
        if title_for_search:
            query1b = f'"{title_for_search}" l:chinese'
            link, sim = self.search_ehentai_single(query1b, candidates, english_title)
            if link:
                return ConversionResult(
                    jm_id=jm_id,
                    title=title,
                    author=author,
                    link=link,
                    source="ehentai",
                    similarity=sim,
                )

        # --- Query 2: Romaji with English substitutions (Jamdict) ---
        print("  → Trying romaji with English substitutions...")
        romaji_eng = to_romaji_with_english(oname)
        if romaji_eng:
            # Truncate to first 4 words
            romaji_eng_words = romaji_eng.split()
            if len(romaji_eng_words) > 4:
                romaji_eng = " ".join(romaji_eng_words[:4])
            query2 = f"{author_romaji} {romaji_eng} l:chinese".strip()
            link, sim = self.search_ehentai_single(query2, candidates, romaji_eng)
            if link:
                return ConversionResult(
                    jm_id=jm_id,
                    title=title,
                    author=author,
                    link=link,
                    source="ehentai",
                    similarity=sim,
                )

        # --- Query 3: English translation (SimplyTranslate) ---
        if not english_title:
            print("  → Trying English translation...")
            translated = translate_to_english(oname, source="ja")
            if translated:
                trans_words = translated.split()
                if len(trans_words) > 4:
                    translated = " ".join(trans_words[:4])
                query3 = f"{author_romaji} {translated} l:chinese".strip()
                link, sim = self.search_ehentai_single(query3, candidates, translated)
                if link:
                    return ConversionResult(
                        jm_id=jm_id,
                        title=title,
                        author=author,
                        link=link,
                        source="ehentai",
                        similarity=sim,
                    )

        # --- Query 3b: Japanese title direct search ---
        # Fallback for cases where romaji conversion is wrong (e.g., 甘喰み -> amabami not kanshoku)
        jp_oname = to_jp_kanji(oname)
        if jp_oname and any(
            "\u3040" <= c <= "\u9fff" for c in jp_oname
        ):  # Has CJK chars
            print("  → Trying Japanese title search...")
            query3b = f"{jp_oname} l:chinese"
            link, sim = self.search_ehentai_single(query3b, candidates, english_title)
            if link:
                return ConversionResult(
                    jm_id=jm_id,
                    title=title,
                    author=author,
                    link=link,
                    source="ehentai",
                    similarity=sim,
                )

        # --- Query 3c: Extract Japanese title from full title ---
        # For cases where oname is English but full title has Japanese (e.g., "Dopuya [author] どぴゅあ")
        jp_from_title = self._extract_jp_title(title)
        if jp_from_title:
            # Clean up: take only the first part before + or numbers
            jp_search = re.sub(r"\s*[+＋].*", "", jp_from_title).strip()
            jp_search = re.sub(r"\s*\d+P.*", "", jp_search).strip()
            jp_search = to_jp_kanji(jp_search)
            if jp_search and len(jp_search) >= 3 and jp_search != jp_oname:
                # Try with author name for better precision
                author_jp = to_jp_kanji(author) if author else ""
                print(f"  → Trying extracted JP title: {author_jp} {jp_search}")
                query3c = f"{author_jp} {jp_search} l:chinese".strip()
                link, sim = self.search_ehentai_single(
                    query3c, candidates, english_title
                )
                if link:
                    return ConversionResult(
                        jm_id=jm_id,
                        title=title,
                        author=author,
                        link=link,
                        source="ehentai",
                        similarity=sim,
                    )

        # --- Query 4: wnacg ---
        print("  → Trying wnacg.com...")
        link, sim = self.search_wnacg(
            oname, candidates, full_title=title, author=author
        )
        if link:
            return ConversionResult(
                jm_id=jm_id,
                title=title,
                author=author,
                link=link,
                source="wnacg",
                similarity=sim,
            )

        # No match found
        return ConversionResult(
            jm_id=jm_id,
            title=title,
            author=author,
            link="",
            source="none",
            similarity=0.0,
        )


def test_conversion():
    """Test conversion with sample IDs."""
    test_ids = [
        "1180203",
        "540930",
        "1192427",
        "1191862",
        "224412",
        "1190464",
        "1060422",
        "1026275",
        "1186623",
        "1132672",
        "280934",
        "403551",
        "259194",
        "364547",
        "118648",
        "347117",
        "304642",
        "265033",
        "270650",
    ]

    converter = JM2EConverter()
    results = []

    for jm_id in test_ids:
        try:
            result = converter.convert(jm_id)
            results.append(result)
            sim_str = f" ({result.similarity:.2f})" if result.similarity > 0 else ""
            print(f"✓ JM{jm_id}: [{result.source}]{sim_str} {result.link}\n")
        except Exception as e:
            print(f"✗ JM{jm_id}: Error - {e}\n")
            results.append(None)

    # Summary
    print("\n" + "=" * 60)
    ehentai_count = sum(1 for r in results if r and r.source == "ehentai")
    wnacg_count = sum(1 for r in results if r and r.source == "wnacg")
    none_count = sum(1 for r in results if r and r.source == "none")
    failed_count = sum(1 for r in results if r is None)

    print(f"E-Hentai: {ehentai_count}")
    print(f"wnacg: {wnacg_count}")
    print(f"None: {none_count}")
    print(f"Failed: {failed_count}")

    return results


if __name__ == "__main__":
    test_conversion()
