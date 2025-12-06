"""
JM2E: JMComic to E-Hentai link converter with fallback chain.

Query flow (4 queries max):
1. E-Hentai: Pure romaji (pykakasi)
2. E-Hentai: Romaji + English keywords (SimplyTranslate for katakana)
3. E-Hentai: English translation (SimplyTranslate API)
4. wnacg: Chinese title direct search

Performance optimizations:
- Caches derived forms (romaji, jp_kanji) per conversion
- Uses shared HTTP client for connection pooling
- Concurrent E-Hentai searches for faster results
"""

import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Optional

import httpx
import opencc_purepy as opencc
import pykakasi
import jmcomic
from ehentai import get_search


# Similarity threshold for matching
SIMILARITY_THRESHOLD = 0.55

# Initialize converters (module-level singletons)
_s2t = opencc.OpenCC("s2t")
_t2jp = opencc.OpenCC("t2jp")
_kks = pykakasi.kakasi()


# Shared HTTP client for connection pooling
_http_client: Optional[httpx.Client] = None


def _get_http_client() -> httpx.Client:
    """Get or create shared HTTP client with connection pooling."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            follow_redirects=True,
        )
    return _http_client


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


@lru_cache(maxsize=256)
def to_jp_kanji(text: str) -> str:
    """Convert Simplified Chinese to Japanese kanji via Traditional Chinese.

    Cached for performance - same text always yields same result.
    """
    result = _t2jp.convert(_s2t.convert(text))
    for trad, jp in EXTRA_CHAR_MAP.items():
        result = result.replace(trad, jp)
    return result


# Pre-compiled regex for special characters
_SPECIAL_CHARS_RE = re.compile(r"[①②③④⑤⑥⑦⑧⑨⑩♡♥☆★◆◇○●～〜]")


@lru_cache(maxsize=256)
def to_romaji(text: str) -> str:
    """Convert Japanese/Chinese text to pure romaji (no spaces).

    Cached for performance - same text always yields same result.
    """
    jp_text = to_jp_kanji(text)
    # Apply romaji-specific character mappings for correct readings
    for orig, repl in ROMAJI_CHAR_MAP.items():
        jp_text = jp_text.replace(orig, repl)
    # Remove special characters
    jp_text = _SPECIAL_CHARS_RE.sub("", jp_text)
    result = _kks.convert(jp_text)
    return "".join([item["hepburn"] for item in result]).lower().replace(" ", "")


@lru_cache(maxsize=256)
def to_romaji_spaced(text: str) -> str:
    """Convert Japanese/Chinese text to romaji with spaces between words.

    Cached for performance - same text always yields same result.
    """
    jp_text = to_jp_kanji(text)
    # Apply romaji-specific character mappings for correct readings
    for orig, repl in ROMAJI_CHAR_MAP.items():
        jp_text = jp_text.replace(orig, repl)
    # Remove special characters (replace with space to preserve word boundaries)
    jp_text = _SPECIAL_CHARS_RE.sub(" ", jp_text)
    result = _kks.convert(jp_text)
    parts = [item["hepburn"] for item in result if item["hepburn"]]
    return " ".join(parts).lower()


def _is_katakana_word(text: str) -> bool:
    """Check if text is primarily katakana (80%+ katakana characters)."""
    if not text:
        return False
    import unicodedata

    katakana_count = sum(1 for c in text if "KATAKANA" in unicodedata.name(c, ""))
    return katakana_count >= len(text) * 0.8


def _translate_katakana_words(katakana_words: list[str]) -> dict[str, str]:
    """Translate multiple katakana words to English using SimplyTranslate API.

    Returns a dict mapping katakana -> English translation.
    Only includes single-word translations (no phrases).
    """
    if not katakana_words:
        return {}

    # Join words with separator that won't appear in translation
    separator = " | "
    combined = separator.join(katakana_words)

    try:
        client = _get_http_client()
        resp = client.get(
            TRANSLATE_API,
            params={"engine": "google", "from": "ja", "to": "en", "text": combined},
        )
        if resp.status_code == 200:
            translated = resp.json().get("translated_text", "")
            # Split back and map
            parts = translated.split("|")
            result = {}
            for i, kata in enumerate(katakana_words):
                if i < len(parts):
                    eng = parts[i].strip().lower()
                    # Only single words, no phrases
                    if eng and " " not in eng and len(eng) >= 3:
                        result[kata] = eng
            return result
    except Exception:
        pass
    return {}


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

    # First pass: collect katakana words
    katakana_words = []
    for seg in segments:
        orig = seg.get("orig", "")
        if orig and _is_katakana_word(orig) and len(orig) >= 2:
            if orig not in katakana_words:
                katakana_words.append(orig)

    # Batch translate katakana words
    katakana_translations = _translate_katakana_words(katakana_words)

    # Second pass: build result
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
            english_word = katakana_translations.get(orig)
            if english_word:
                result_parts.append(english_word)
                continue

        # Otherwise use romaji
        if hepburn:
            result_parts.append(hepburn)

    return " ".join(result_parts).lower()


def translate_to_english(text: str, source: str = "ja") -> Optional[str]:
    """Translate Japanese/Chinese text to English using SimplyTranslate API.

    Uses shared HTTP client for connection pooling.
    """
    try:
        client = _get_http_client()
        resp = client.get(
            TRANSLATE_API,
            params={"engine": "google", "from": source, "to": "en", "text": text},
        )
        if resp.status_code == 200:
            return resp.json().get("translated_text")
    except Exception as e:
        print(f"  Translation error: {e}")
    return None


# Pre-compiled regex for normalization
_CJK_NORM_RE = re.compile(r"[^a-z0-9\u3040-\u9faf]")
_ROMAJI_NORM_RE = re.compile(r"[^a-z0-9]")


@lru_cache(maxsize=512)
def normalize_cjk(text: str) -> str:
    """Normalize for CJK comparison. Cached for performance."""
    jp_text = to_jp_kanji(text)
    return _CJK_NORM_RE.sub("", jp_text.lower())


@lru_cache(maxsize=512)
def normalize_romaji(text: str) -> str:
    """Normalize for romaji comparison. Cached for performance."""
    return _ROMAJI_NORM_RE.sub("", text.lower())


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


@dataclass
class SearchContext:
    """Pre-computed search context for a single conversion.

    Caches all derived forms to avoid repeated computation during search.
    """

    oname: str
    author: str
    title: str
    candidates: list[str]
    description: str

    # Pre-computed derived forms (computed once)
    author_romaji: str = ""
    jp_oname: str = ""
    romaji_spaced: str = ""
    romaji_with_english: str = ""
    english_title: Optional[str] = None
    author_jp: str = ""

    # Pre-computed normalized forms for candidates
    candidate_norms: list[str] = field(default_factory=list)
    candidate_romajis: list[str] = field(default_factory=list)

    def __post_init__(self):
        """Compute all derived forms once."""
        # Author forms
        if self.author:
            self.author_romaji = to_romaji(self.author)
            self.author_jp = to_jp_kanji(self.author)

        # Title forms
        self.jp_oname = to_jp_kanji(self.oname)
        self.romaji_spaced = to_romaji_spaced(self.oname)
        self.romaji_with_english = to_romaji_with_english(self.oname)

        # Pre-compute normalized forms for all candidates
        for c in self.candidates:
            self.candidate_norms.append(normalize_cjk(c))
            self.candidate_romajis.append(to_romaji(c))


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

        # Remove common tags at the end (single pass with greedy matching)
        clean = re.sub(r"(\s*\[[^\]]*\])+\s*$", "", description)

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

    def convert(self, jm_id: str, concurrent: bool = True) -> ConversionResult:
        """Convert JMComic ID to link with multi-query flow.

        Args:
            jm_id: JMComic album ID
            concurrent: If True, run initial E-Hentai queries concurrently for speed

        Query flow:
        1. E-Hentai: Best available title (English from desc/title, or romaji)
        1b. E-Hentai: Quoted title without author
        2. E-Hentai: Romaji + English keywords (Jamdict)
        3. E-Hentai: English translation (SimplyTranslate) - if no English title
        3b. E-Hentai: Japanese title direct search
        3c. E-Hentai: Extracted JP title from full title
        4. wnacg: Chinese title search (fallback)
        """
        info = self.get_jm_info(jm_id)
        title = info["title"]
        author = info["author"]
        oname = info["oname"]
        candidates = info["candidates"]
        description = info.get("description", "")

        print(f"JM{jm_id}: {title}")

        # Create search context with pre-computed values
        ctx = SearchContext(
            oname=oname,
            author=author,
            title=title,
            candidates=candidates.copy(),
            description=description,
        )

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
        ctx.english_title = english_title

        if english_title and english_title not in candidates:
            candidates.append(english_title)

        # Build search queries
        queries: list[
            tuple[str, str, Optional[str]]
        ] = []  # (query, name, english_hint)

        # Query 1: Best available title
        if english_title:
            title_for_search = clean_for_eh_search(english_title)
        else:
            title_for_search = ctx.romaji_spaced

        # Truncate to first 4 words
        words = title_for_search.split()
        if len(words) > 4:
            title_for_search = " ".join(words[:4])

        query1 = f"{ctx.author_romaji} {title_for_search} l:chinese".strip()
        queries.append((query1, "query1", english_title))

        # Query 1b: Quoted title without author
        if title_for_search:
            query1b = f'"{title_for_search}" l:chinese'
            queries.append((query1b, "query1b", english_title))

        # Query 2: Romaji with English substitutions
        romaji_eng = ctx.romaji_with_english
        if romaji_eng:
            romaji_eng_words = romaji_eng.split()
            if len(romaji_eng_words) > 4:
                romaji_eng = " ".join(romaji_eng_words[:4])
            query2 = f"{ctx.author_romaji} {romaji_eng} l:chinese".strip()
            if query2 != query1:  # Avoid duplicate
                queries.append((query2, "query2", romaji_eng))

        if concurrent and len(queries) >= 2:
            # Run first batch of queries concurrently
            result = self._search_concurrent(
                queries, candidates, ctx, jm_id, title, author
            )
            if result:
                return result
        else:
            # Sequential search
            for query, name, eng_hint in queries:
                link, sim = self.search_ehentai_single(query, candidates, eng_hint)
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
                query3 = f"{ctx.author_romaji} {translated} l:chinese".strip()
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
        jp_oname = ctx.jp_oname
        if jp_oname and any("\u3040" <= c <= "\u9fff" for c in jp_oname):
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
        jp_from_title = self._extract_jp_title(title)
        if jp_from_title:
            jp_search = re.sub(r"\s*[+＋].*", "", jp_from_title).strip()
            jp_search = re.sub(r"\s*\d+P.*", "", jp_search).strip()
            jp_search = to_jp_kanji(jp_search)
            if jp_search and len(jp_search) >= 3 and jp_search != jp_oname:
                print(f"  → Trying extracted JP title: {ctx.author_jp} {jp_search}")
                query3c = f"{ctx.author_jp} {jp_search} l:chinese".strip()
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

    def _search_concurrent(
        self,
        queries: list[tuple[str, str, Optional[str]]],
        candidates: list[str],
        ctx: SearchContext,
        jm_id: str,
        title: str,
        author: str,
    ) -> Optional[ConversionResult]:
        """Run multiple E-Hentai searches concurrently.

        Returns the first successful result or None.
        """
        results: dict[str, tuple[Optional[str], float]] = {}

        def search_one(
            query_info: tuple[str, str, Optional[str]],
        ) -> tuple[str, Optional[str], float]:
            query, name, eng_hint = query_info
            link, sim = self.search_ehentai_single(query, candidates, eng_hint)
            return name, link, sim

        # Run searches concurrently
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(search_one, q): q[1] for q in queries}

            for future in as_completed(futures):
                name, link, sim = future.result()
                results[name] = (link, sim)

                # Early exit if we found a good match
                if link and sim >= SIMILARITY_THRESHOLD:
                    # Cancel remaining futures (best effort)
                    for f in futures:
                        f.cancel()
                    return ConversionResult(
                        jm_id=jm_id,
                        title=title,
                        author=author,
                        link=link,
                        source="ehentai",
                        similarity=sim,
                    )

        # Check results in priority order
        for query, name, _ in queries:
            if name in results:
                link, sim = results[name]
                if link:
                    return ConversionResult(
                        jm_id=jm_id,
                        title=title,
                        author=author,
                        link=link,
                        source="ehentai",
                        similarity=sim,
                    )

        return None


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
