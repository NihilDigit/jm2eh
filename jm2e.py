"""
JM2E: JMComic to E-Hentai link converter with fallback chain.
"""

import re
import urllib.parse
from dataclasses import dataclass
from typing import Optional

import jmcomic
from ehentai import get_search


@dataclass
class ConversionResult:
    """Result of JMComic ID to link conversion."""

    jm_id: str
    title: str
    author: str
    link: str
    source: str  # 'ehentai', 'hitomi', 'google'

    def __str__(self):
        return f"[{self.source.upper()}] {self.link}"


class JM2EConverter:
    """Converts JMComic IDs to E-Hentai links with fallback."""

    def __init__(self):
        self.jm_option = jmcomic.JmOption.default()
        self.jm_client = self.jm_option.new_jm_client()

    def get_jm_info(self, jm_id: str) -> tuple[str, str]:
        """Get title and author from JMComic ID."""
        album = self.jm_client.get_album_detail(jm_id)
        return album.title, album.author

    def clean_title_for_search(self, title: str) -> str:
        """Clean title for search: remove translation tags, special chars, etc."""
        # Remove common tags like [xxx], (xxx) at start/end
        cleaned = re.sub(r"^\s*[\[\(][^\]\)]*[\]\)]\s*", "", title)
        cleaned = re.sub(r"\s*[\[\(][^\]\)]*[\]\)]$", "", cleaned)
        # Remove DL版, 中国翻译, etc.
        cleaned = re.sub(
            r"\[DL版\]|\[中国翻译\]|\[中國翻譯\]|\[汉化\]|\[漢化\]",
            "",
            cleaned,
            flags=re.IGNORECASE,
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned

    def extract_japanese_title(self, title: str) -> Optional[str]:
        """Extract Japanese title part from the full title."""
        # Match Japanese characters (hiragana, katakana, kanji)
        jp_match = re.search(
            r"[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9faf\u3000-\u303f]+", title
        )
        if jp_match:
            return jp_match.group(0)
        return None

    def search_ehentai(self, title: str, author: str) -> Optional[str]:
        """Search E-Hentai for the manga and return gallery URL if found."""
        search_queries = []

        # Strategy 1: Author name (most reliable)
        if author:
            search_queries.append(author)

        # Strategy 2: Cleaned title
        cleaned_title = self.clean_title_for_search(title)
        if cleaned_title:
            search_queries.append(cleaned_title)

        # Strategy 3: Japanese title part
        jp_title = self.extract_japanese_title(title)
        if jp_title and jp_title != cleaned_title:
            search_queries.append(jp_title)

        for query in search_queries:
            try:
                print(f"  Searching E-Hentai: {query}")
                page = get_search(query, direct=True)
                if page.gl_table:
                    # Return the first result
                    return page.gl_table[0].view_url
            except Exception as e:
                print(f"  E-Hentai search error: {e}")
                continue

        return None

    def get_hitomi_link(self, title: str, author: str) -> str:
        """Generate Hitomi.la search URL."""
        cleaned_title = self.clean_title_for_search(title)
        query = cleaned_title
        if author:
            query = f"{author} {cleaned_title}"
        encoded_query = urllib.parse.quote(query)
        return f"https://hitomi.la/search.html?{encoded_query}"

    def get_google_link(self, title: str, author: str) -> str:
        """Generate Google search URL for E-Hentai."""
        cleaned_title = self.clean_title_for_search(title)
        query = f'site:e-hentai.org "{cleaned_title}"'
        if author:
            query = f'site:e-hentai.org "{author}" "{cleaned_title}"'
        encoded_query = urllib.parse.quote(query)
        return f"https://www.google.com/search?q={encoded_query}"

    def convert(self, jm_id: str) -> ConversionResult:
        """Convert JMComic ID to link with fallback chain."""
        # Get JMComic info
        title, author = self.get_jm_info(jm_id)
        print(f"JM{jm_id}: {title} by {author}")

        # Try E-Hentai first
        ehentai_link = self.search_ehentai(title, author)
        if ehentai_link:
            return ConversionResult(
                jm_id=jm_id,
                title=title,
                author=author,
                link=ehentai_link,
                source="ehentai",
            )

        # Fallback to Hitomi
        hitomi_link = self.get_hitomi_link(title, author)
        return ConversionResult(
            jm_id=jm_id, title=title, author=author, link=hitomi_link, source="hitomi"
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
            print(f"✓ JM{jm_id}: [{result.source}] {result.link}\n")
        except Exception as e:
            print(f"✗ JM{jm_id}: Error - {e}\n")
            results.append(None)

    # Summary
    print("\n" + "=" * 60)
    ehentai_count = sum(1 for r in results if r and r.source == "ehentai")
    hitomi_count = sum(1 for r in results if r and r.source == "hitomi")
    google_count = sum(1 for r in results if r and r.source == "google")
    failed_count = sum(1 for r in results if r is None)

    print(f"E-Hentai: {ehentai_count}")
    print(f"Hitomi: {hitomi_count}")
    print(f"Google: {google_count}")
    print(f"Failed: {failed_count}")

    return results


if __name__ == "__main__":
    test_conversion()
