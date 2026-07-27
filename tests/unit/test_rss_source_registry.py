from pathlib import Path

import yaml

from kokoro_link.domain.value_objects.rss_category import CANONICAL_RSS_CATEGORIES


def _bundled_sources() -> list[dict]:
    seed_path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "kokoro_link"
        / "data"
        / "rss_sources.yaml"
    )
    payload = yaml.safe_load(seed_path.read_text(encoding="utf-8"))
    return payload["sources"]


def test_bundled_rss_sources_use_canonical_categories() -> None:
    sources = _bundled_sources()
    categories = {cat.value for cat in CANONICAL_RSS_CATEGORIES}

    assert sources
    assert {source["category"] for source in sources} <= categories


def test_bundled_rss_sources_have_unique_ids_and_urls() -> None:
    sources = _bundled_sources()
    ids = [source["id"] for source in sources]
    urls = [source["feed_url"] for source in sources]

    assert len(ids) == len(set(ids))
    assert len(urls) == len(set(urls))


def test_bundled_rss_sources_include_official_emergency_alerts() -> None:
    sources = _bundled_sources()

    assert any(
        source["id"] == "ncdr-all-alerts"
        and source["category"] == "emergency"
        and source["locale"] == "zh-TW"
        for source in sources
    )


def test_bundled_rss_sources_include_social_trend_first_wave() -> None:
    sources = {source["id"]: source for source in _bundled_sources()}
    expected = {
        "kym-newsfeed": ("culture", "en-US"),
        "kym-confirmed-memes": ("culture", "en-US"),
        "reddit-memes": ("culture", "en-US"),
        "reddit-programmerhumor": ("tech", "en-US"),
        "google-trends-tw": ("culture", "zh-TW"),
        "google-news-meme-tw": ("culture", "zh-TW"),
        "ptt-c-chat": ("anime", "zh-TW"),
        "ptt-mobilecomm": ("tech", "zh-TW"),
        "ptt-pc-shopping": ("tech", "zh-TW"),
    }

    for source_id, (category, locale) in expected.items():
        source = sources[source_id]
        assert source["category"] == category
        assert source["locale"] == locale
        assert source["enabled"] is True


def test_bundled_rss_sources_include_tech_and_status_first_wave() -> None:
    sources = {source["id"]: source for source in _bundled_sources()}
    expected_ids = {
        "reddit-technology",
        "reddit-apple",
        "apple-newsroom",
        "the-verge",
        "engadget",
        "techcrunch",
        "9to5mac",
        "lobsters",
        "github-blog",
        "cloudflare-blog",
        "cloudflare-status",
        "github-status",
        "openai-status",
        "discord-status",
    }

    for source_id in expected_ids:
        source = sources[source_id]
        assert source["category"] == "tech"
        assert source["enabled"] is True


def test_taiwan_bound_sources_declare_region_tw() -> None:
    """Outlets whose reporting only lands for people living in Taiwan must
    be tagged, or a hosted player abroad keeps getting local Taiwanese news
    (the whole point of the region dimension)."""
    sources = {source["id"]: source for source in _bundled_sources()}
    taiwan_bound = {
        "cna-realtime",
        "ncdr-all-alerts",
        "ithome-news",
        "google-trends-tw",
        "google-news-meme-tw",
        "ptt-c-chat",
        "ptt-mobilecomm",
        "ptt-pc-shopping",
        "gnn-gamer",
        "udn-life",
    }

    for source_id in taiwan_bound:
        assert sources[source_id].get("region") == "TW", source_id


def test_globally_readable_sources_declare_no_region() -> None:
    """Region is opt-in. Tagging a source that anyone could enjoy — an
    international wire, or a Japanese outlet whose anime coverage has a
    worldwide readership — would silently shrink every player's pool."""
    sources = {source["id"]: source for source in _bundled_sources()}
    global_ids = {
        "bbc-world",
        "hackernews-front",
        "4gamer",
        "animate-times",
        "oricon-entertainment",
        "nature-news",
        "the-verge",
        "techcrunch",
        # Hosted global pack: en-US newsrooms, but the coverage is for
        # readers anywhere — publishing language is not a region.
        "npr-top-stories",
        "ars-technica",
    }

    for source_id in global_ids:
        assert sources[source_id].get("region") is None, source_id


def test_japan_bound_sources_declare_region_jp() -> None:
    """The hosted JP pack: outlets covering Japanese domestic life. A
    player in Taiwan gets nothing from NHK's headline rundown, so these
    are curated only into JP players' pools (mirror of the TW rule)."""
    sources = {source["id"]: source for source in _bundled_sources()}
    japan_bound = {
        "nhk-easy",
        "nhk-science",
        "itmedia-news",
    }

    for source_id in japan_bound:
        assert sources[source_id].get("region") == "JP", source_id


def test_bundled_rss_sources_include_hosted_default_pack() -> None:
    """The six feeds the owner picked as the hosted default. Two of them
    (NHK 主要ニュース via ``nhk-easy``, BBC World) already shipped, so the
    pack is asserted by feed URL — a second entry for the same URL would
    poll it twice and is caught by the uniqueness test above."""
    sources = {source["id"]: source for source in _bundled_sources()}
    expected = {
        "nhk-easy": (
            "https://www3.nhk.or.jp/rss/news/cat0.xml", "news", "ja-JP",
        ),
        "nhk-science": (
            "https://www3.nhk.or.jp/rss/news/cat3.xml", "science", "ja-JP",
        ),
        "itmedia-news": (
            "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml",
            "tech", "ja-JP",
        ),
        "npr-top-stories": (
            "https://feeds.npr.org/1001/rss.xml", "news", "en-US",
        ),
        "bbc-world": (
            "https://feeds.bbci.co.uk/news/world/rss.xml", "news", "en-GB",
        ),
        "ars-technica": (
            "https://feeds.arstechnica.com/arstechnica/index", "tech", "en-US",
        ),
    }

    for source_id, (feed_url, category, locale) in expected.items():
        source = sources[source_id]
        assert source["feed_url"] == feed_url, source_id
        assert source["category"] == category, source_id
        assert source["locale"] == locale, source_id
        assert source["enabled"] is True, source_id


def test_bundled_region_codes_are_upper_case_two_letter() -> None:
    for source in _bundled_sources():
        region = source.get("region")
        if region is None:
            continue
        assert region == region.upper()
        assert len(region) == 2
