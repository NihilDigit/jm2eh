"""
JM2E: JMComic to E-Hentai link converter with fallback chain.

Search strategy:
1. Get JM album's oname (original title) and author
2. Search E-Hentai by author (romaji)
3. Match results using dual strategy:
   - Direct match: oname vs EH title's Chinese/Japanese part
   - Romaji match: oname (romaji) vs EH title's romaji part
4. Return best match above threshold, otherwise fallback to Hitomi
"""

import re
import urllib.parse
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

import opencc
import pykakasi
import jmcomic
from ehentai import get_search

# Similarity threshold for matching
SIMILARITY_THRESHOLD = 0.6

# Initialize converters
_cc = opencc.OpenCC("s2t")  # Simplified -> Traditional (closer to Japanese kanji)
_kks = pykakasi.kakasi()


def to_romaji(text: str) -> str:
    """Convert Japanese/Chinese text to romaji."""
    text = _cc.convert(text)
    result = _kks.convert(text)
    return "".join([item["hepburn"] for item in result]).lower().replace(" ", "")


def normalize_cjk(text: str) -> str:
    """Normalize for CJK comparison."""
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


def calc_match_score(jm_oname: str, eh_title: str) -> tuple[float, str]:
    """Calculate best match score between JM oname and EH title.

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
    source: str  # 'ehentai', 'hitomi'
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
        return {
            "title": album.title,
            "author": album.author,
            "oname": getattr(album, "oname", "") or album.title,
        }

    def search_ehentai(self, oname: str, author: str) -> tuple[Optional[str], float]:
        """Search E-Hentai and find best matching gallery.

        Returns: (url, similarity_score) or (None, 0)
        """
        author_romaji = to_romaji(author) if author else ""

        print(f"  oname: {oname}")
        if author:
            print(f"  author: {author} -> {author_romaji}")

        best_match = None
        best_score = 0.0
        best_method = ""

        # Search queries to try
        search_queries = []
        if author_romaji:
            search_queries.append(f"{author_romaji} l:chinese")
            search_queries.append(author_romaji)
        search_queries.append(f"{oname} l:chinese")

        for query in search_queries:
            try:
                print(f"  Searching: {query}")
                page = get_search(query, direct=True)

                for gallery in page.gl_table:
                    score, method = calc_match_score(oname, gallery.name)

                    if score > best_score:
                        best_score = score
                        best_match = gallery
                        best_method = method

                    # Early exit on high confidence match
                    if score >= 0.95:
                        print(
                            f"  ✓ Match ({method}): {score:.2f} | {gallery.name[:60]}"
                        )
                        return gallery.view_url, score

            except Exception as e:
                print(f"  Search error: {e}")
                continue

            # Stop if we found a good match
            if best_score >= SIMILARITY_THRESHOLD:
                break

        if best_match and best_score >= SIMILARITY_THRESHOLD:
            print(
                f"  ✓ Best ({best_method}): {best_score:.2f} | {best_match.name[:60]}"
            )
            return best_match.view_url, best_score

        if best_match:
            print(
                f"  ✗ Below threshold ({best_method}): {best_score:.2f} | {best_match.name[:60]}"
            )

        return None, best_score

    def get_hitomi_link(self, oname: str, author: str) -> str:
        """Generate Hitomi.la search URL."""
        query = oname
        if author:
            query = f"{author} {oname}"
        encoded_query = urllib.parse.quote(query)
        return f"https://hitomi.la/search.html?{encoded_query}"

    def convert(self, jm_id: str) -> ConversionResult:
        """Convert JMComic ID to link with fallback chain."""
        info = self.get_jm_info(jm_id)
        title = info["title"]
        author = info["author"]
        oname = info["oname"]

        print(f"JM{jm_id}: {title}")

        # Try E-Hentai
        ehentai_link, sim = self.search_ehentai(oname, author)
        if ehentai_link:
            return ConversionResult(
                jm_id=jm_id,
                title=title,
                author=author,
                link=ehentai_link,
                source="ehentai",
                similarity=sim,
            )

        # Fallback to Hitomi
        hitomi_link = self.get_hitomi_link(oname, author)
        return ConversionResult(
            jm_id=jm_id,
            title=title,
            author=author,
            link=hitomi_link,
            source="hitomi",
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
    hitomi_count = sum(1 for r in results if r and r.source == "hitomi")
    failed_count = sum(1 for r in results if r is None)

    print(f"E-Hentai: {ehentai_count}")
    print(f"Hitomi: {hitomi_count}")
    print(f"Failed: {failed_count}")

    return results


if __name__ == "__main__":
    test_conversion()
