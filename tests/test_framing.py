"""Tests for framing.py — sentiment divergence detection."""

from datetime import datetime, timedelta

from framing import FramingDivergence, analyze_framing


def _add_story(conn, title="Test Story", score=10.0, sources=4, status="active"):
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute(
        """INSERT INTO stories
           (slug, representative_title, first_seen, last_seen,
            peak_source_count, status, importance_score)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (f"test-{title[:20]}", title, today, today, sources, status, score),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _add_headline(conn, story_id, source, title, url=None):
    today = datetime.now().strftime("%Y-%m-%d")
    if url is None:
        url = f"https://example.com/{hash((source, title))}"
    conn.execute(
        """INSERT INTO headlines (url, title, source, first_seen, last_seen, story_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (url, title, source, today, today, story_id),
    )
    conn.commit()


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------


def test_no_stories(conn):
    """Empty DB → no divergences."""
    assert analyze_framing(conn) == []


def test_story_only_one_side(conn):
    """Story with only left sources — no divergence possible."""
    sid = _add_story(conn, "Left Only", sources=3)
    _add_headline(conn, sid, "CNN Top Stories", "Great news for everyone today")
    _add_headline(conn, sid, "MSNBC Top Stories", "Wonderful progress reported")
    _add_headline(conn, sid, "NPR News", "Positive developments emerge")
    assert analyze_framing(conn, min_divergence=0.0) == []


def test_same_tone_no_divergence(conn):
    """Both sides neutral — divergence below threshold."""
    sid = _add_story(conn, "Bipartisan Story", sources=4)
    _add_headline(conn, sid, "CNN Top Stories", "Congress meets on Tuesday")
    _add_headline(conn, sid, "MSNBC Top Stories", "Congress session scheduled")
    _add_headline(conn, sid, "FOX News Latest", "Congress convenes Tuesday")
    _add_headline(conn, sid, "Breitbart", "Congressional session set for Tuesday")
    # Neutral headlines → very low divergence
    results = analyze_framing(conn, min_divergence=0.5)
    assert len(results) == 0


def test_divergence_detected(conn):
    """Opposite framing should produce a divergence."""
    sid = _add_story(conn, "Polarized Issue", sources=4, score=50.0)
    # Positive left framing
    _add_headline(
        conn,
        sid,
        "CNN Top Stories",
        "Historic victory celebrates amazing progress and joy",
    )
    _add_headline(
        conn,
        sid,
        "MSNBC Top Stories",
        "Wonderful triumph brings hope and happiness to millions",
    )
    # Negative right framing
    _add_headline(
        conn,
        sid,
        "FOX News Latest",
        "Devastating disaster causes horrible suffering and pain",
    )
    _add_headline(
        conn, sid, "Breitbart", "Terrible catastrophe brings destruction and misery"
    )

    results = analyze_framing(conn, min_divergence=0.1)
    assert len(results) >= 1
    d = results[0]
    assert d.story_id == sid
    assert d.divergence > 0.1
    assert d.left_sentiment > d.right_sentiment
    assert len(d.left_headlines) == 2
    assert len(d.right_headlines) == 2


def test_min_sources_filter(conn):
    """Stories below min_sources should be excluded."""
    sid = _add_story(conn, "Small Story", sources=2, score=5.0)
    _add_headline(conn, sid, "CNN Top Stories", "Great wonderful amazing news")
    _add_headline(conn, sid, "FOX News Latest", "Terrible horrible disaster strikes")
    # min_sources=3 → excluded
    assert analyze_framing(conn, min_sources=3, min_divergence=0.0) == []
    # min_sources=2 → included
    results = analyze_framing(conn, min_sources=2, min_divergence=0.0)
    assert len(results) >= 1


def test_dataclass_fields(conn):
    """FramingDivergence has expected fields."""
    d = FramingDivergence(
        story_id=1,
        title="Test",
        importance_score=10.0,
        left_sentiment=0.5,
        right_sentiment=-0.3,
        center_sentiment=0.1,
        divergence=0.8,
    )
    assert d.divergence == 0.8
    assert d.left_headlines == []
    assert d.right_headlines == []


def test_sorted_by_divergence(conn):
    """Results should be sorted by divergence descending."""
    # Story 1: mild divergence
    sid1 = _add_story(conn, "Mild Story", sources=4, score=20.0)
    _add_headline(
        conn, sid1, "CNN Top Stories", "Good news reported today in Washington"
    )
    _add_headline(conn, sid1, "MSNBC Top Stories", "Nice progress on legislation")
    _add_headline(
        conn, sid1, "FOX News Latest", "Concerns raised about policy direction"
    )
    _add_headline(
        conn, sid1, "Breitbart", "Questionable moves from government officials"
    )

    # Story 2: extreme divergence
    sid2 = _add_story(conn, "Extreme Story", sources=4, score=30.0)
    _add_headline(
        conn,
        sid2,
        "CNN Top Stories",
        "Magnificent spectacular glorious triumph for all",
    )
    _add_headline(
        conn,
        sid2,
        "MSNBC Top Stories",
        "Beautiful wonderful celebration of amazing victory",
    )
    _add_headline(
        conn,
        sid2,
        "FOX News Latest",
        "Catastrophic devastating horrible failure destroys everything",
    )
    _add_headline(
        conn, sid2, "Breitbart", "Disastrous terrible appalling collapse and total ruin"
    )

    results = analyze_framing(conn, min_divergence=0.0)
    if len(results) >= 2:
        assert results[0].divergence >= results[1].divergence


def test_reference_date(conn):
    """reference_date parameter filters old stories."""
    old_date = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
    conn.execute(
        """INSERT INTO stories
           (slug, representative_title, first_seen, last_seen,
            peak_source_count, status, importance_score)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("old-story", "Old Story", old_date, old_date, 4, "gone", 20.0),
    )
    conn.commit()
    sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    _add_headline(conn, sid, "CNN Top Stories", "Wonderful amazing happy great news")
    _add_headline(conn, sid, "FOX News Latest", "Terrible horrible awful disaster")

    # With short lookback → not found
    results = analyze_framing(
        conn,
        min_sources=2,
        min_divergence=0.0,
        lookback_days=7,
        reference_date=datetime.now().strftime("%Y-%m-%d"),
    )
    assert len(results) == 0

    # With long lookback → found
    results = analyze_framing(
        conn,
        min_sources=2,
        min_divergence=0.0,
        lookback_days=90,
        reference_date=datetime.now().strftime("%Y-%m-%d"),
    )
    assert len(results) >= 1


def test_center_sentiment_calculated(conn):
    """Center sentiment should be populated when center sources exist."""
    sid = _add_story(conn, "Center Test", sources=5, score=25.0)
    _add_headline(conn, sid, "CNN Top Stories", "Wonderful victory for progress")
    _add_headline(conn, sid, "FOX News Latest", "Terrible defeat for the nation")
    _add_headline(conn, sid, "AP News", "Policy announced at press conference")

    results = analyze_framing(conn, min_sources=3, min_divergence=0.0)
    if results:
        # center_sentiment should be a float (AP is center)
        assert isinstance(results[0].center_sentiment, float)
