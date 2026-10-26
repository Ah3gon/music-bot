"""Автотесты логики бота без Discord, Lavalink и базы. Запускаются из check.py.
Каждый тест — функция test_*; провал — assert с понятным сообщением."""
import asyncio
import traceback

import helpers
import lyrics
import playback
import views


class FakeTrack:
    def __init__(self, title, source="deezer", length=200_000):
        self.title = self.identifier = self.encoded = title
        self.source = source
        self.length = length

    def __repr__(self):
        return self.title


class FakeQueue(list):
    def clear(self):
        del self[:]

    async def put_wait(self, item):
        self.append(item)


class FakePlayer:
    def __init__(self, guild_id):
        self.guild = type("G", (), {"id": guild_id})()
        self.queue = FakeQueue()


def test_vote_threshold_rounds_up():
    v = type("V", (), {"required_percent": 50})()
    got = [views.VoteSkipView.needed_votes(v, n) for n in (1, 2, 3, 4, 5)]
    assert got == [1, 1, 2, 2, 3], f"голосов при 50% для 1..5 слушателей: {got}"


def test_preview_filter():
    assert playback.is_preview(FakeTrack("x", "soundcloud", 30_000)), "превью SoundCloud не распознано"
    assert not playback.is_preview(FakeTrack("x", "deezer", 30_000)), "короткий трек Deezer принят за превью"
    assert not playback.is_preview(FakeTrack("x", "soundcloud", 180_000)), "полный трек SoundCloud принят за превью"
    pick = playback.pick_full_track([FakeTrack("prev", "soundcloud", 30_000), FakeTrack("full", "soundcloud")])
    assert pick is not None and pick.title == "full", f"pick_full_track выбрал {pick}"


def test_default_search_source_is_deezer():
    assert playback.search_source_for("deezer") == "dzsearch"
    assert playback.search_source_for("") == "dzsearch", "пустой источник должен означать Deezer"
    assert playback.search_source_for("неизвестный") == "dzsearch"


def test_lyrics_title_parsing():
    cases = [
        (("Get Lucky (Radio Edit - feat. Pharrell Williams and Nile Rodgers)", "Daft Punk"), ("Daft Punk", "Get Lucky")),
        (("Daft Punk - Get Lucky (Official Video)", ""), ("Daft Punk", "Get Lucky")),
        (("Daft Punk - Get Lucky Official Music Video", ""), ("Daft Punk", "Get Lucky")),
        (("Слова-паразиты", ""), ("", "Слова-паразиты")),
        (("Music Sounds Better With You", "Stardust"), ("Stardust", "Music Sounds Better With You")),
        (("Lift Me Up", "Rihanna"), ("Rihanna", "Lift Me Up")),
        (("Bohemian Rhapsody - Remastered 2011", "Queen"), ("Queen", "Bohemian Rhapsody")),
        (("Eminem - Love The Way You Lie ft. Rihanna", ""), ("Eminem", "Love The Way You Lie")),
        (("Sicko Mode", "Travis Scott, Drake"), ("Travis Scott", "Sicko Mode")),
    ]
    for args, want in cases:
        got = lyrics._clean_title(*args)
        assert got == want, f"_clean_title{args} = {got}, ожидалось {want}"


def test_fair_queue_round_robin():
    async def scenario():
        p = FakePlayer(guild_id=-1)
        await helpers.add_tracks_fairly(p, [FakeTrack("A1"), FakeTrack("A2"), FakeTrack("A3")], 10, True)
        await helpers.add_tracks_fairly(p, [FakeTrack("B1")], 20, True)
        await helpers.add_tracks_fairly(p, [FakeTrack("C1"), FakeTrack("C2")], 30, True)
        await helpers.add_tracks_fairly(p, [FakeTrack("B2")], 20, True)
        return [tr.title for tr in p.queue]
    try:
        got = asyncio.run(scenario())
    finally:
        helpers.track_user_map.pop(-1, None)
    want = ["A1", "B1", "C1", "A2", "B2", "C2", "A3"]
    assert got == want, f"справедливая очередь: {got}, ожидалось {want}"


def test_fair_queue_disabled_appends():
    async def scenario():
        p = FakePlayer(guild_id=-2)
        await helpers.add_tracks_fairly(p, [FakeTrack("A1"), FakeTrack("A2")], 10, False)
        await helpers.add_tracks_fairly(p, [FakeTrack("B1")], 20, False)
        return [tr.title for tr in p.queue]
    try:
        got = asyncio.run(scenario())
    finally:
        helpers.track_user_map.pop(-2, None)
    assert got == ["A1", "A2", "B1"], f"без справедливой очереди: {got}"


def test_fair_queue_keeps_current_track_owner():
    # Статистика пишется, когда трек доиграл, — метка играющего не должна пропасть
    async def scenario():
        p = FakePlayer(guild_id=-3)
        p.current = FakeTrack("NOW")
        helpers.tag_track(-3, p.current, 10)
        await helpers.add_tracks_fairly(p, [FakeTrack("A1"), FakeTrack("A2")], 10, True)
        await helpers.add_tracks_fairly(p, [FakeTrack("B1")], 20, True)
        return helpers.get_track_owner(-3, p.current)
    try:
        owner = asyncio.run(scenario())
    finally:
        helpers.track_user_map.pop(-3, None)
    assert owner == 10, f"владелец играющего трека потерялся: {owner}"


def test_yandex_removed():
    assert all("yandex" not in name for name, _ in playback.ALT_SOURCES), "Яндекс в цепочке автоподмены"
    assert "yandex" not in views.PANEL_CHOICES["default_search_source"][1], "Яндекс в панели"


def test_load_saved_track():
    calls = []

    async def fake_search(query, source, **kw):
        calls.append(query)
        if query.startswith("https://www.deezer.com/"):
            return [FakeTrack("по ссылке")], None
        if query.startswith("https://"):
            return None, Exception("не открылась")
        return [FakeTrack("превью", "soundcloud", 30_000), FakeTrack("по названию")], None

    real = playback.search_with_node_fallback
    playback.search_with_node_fallback = fake_search
    try:
        run = lambda title, uri: asyncio.run(playback.load_saved_track(title, uri, "dzsearch"))
        got = run("Song", "https://www.deezer.com/track/1")
        assert got.title == "по ссылке" and calls == ["https://www.deezer.com/track/1"], \
            f"рабочая ссылка: {got}, запросы {calls}"
        calls.clear()
        got = run("Song", "https://www.youtube.com/watch?v=x")
        assert got.title == "по названию" and calls == ["Song"], \
            f"ссылку на YouTube надо пропускать: {got}, запросы {calls}"
        calls.clear()
        got = run("Song", "https://soundcloud.com/a/b")
        assert got.title == "по названию" and calls == ["https://soundcloud.com/a/b", "Song"], \
            f"неоткрывшаяся ссылка → поиск по названию: {got}, запросы {calls}"
        calls.clear()
        assert run("", "") is None and calls == [], f"пустая запись: запросы {calls}"
    finally:
        playback.search_with_node_fallback = real


def test_format_duration():
    assert helpers.format_duration(0) == "0:00"
    assert helpers.format_duration(65_000) == "1:05"
    assert helpers.format_duration(3_725_000) == "1:02:05"


def run() -> list:
    """Запускает все test_* и возвращает список провалов (пустой — всё хорошо)."""
    failures = []
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as e:
                failures.append(f"{name}: {e}")
            except Exception as e:
                failures.append(f"{name}: {type(e).__name__}: {e} @ "
                                f"{traceback.extract_tb(e.__traceback__)[-1].lineno}")
    return failures
