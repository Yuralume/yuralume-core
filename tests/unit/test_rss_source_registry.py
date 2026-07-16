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
