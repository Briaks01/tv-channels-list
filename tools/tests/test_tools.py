"""Тесты чистой логики скриптов tools/. Сеть не нужна.

Запуск: python3 -I -m unittest discover -s tools/tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import epg_ids  # noqa: E402
import prepare  # noqa: E402


def channel(name, url, hls=True, interlaced=False, height=1080, group="Развлекательные"):
    return {"name": name, "url": url, "attrs": {}, "ok": True, "hls": hls,
            "interlaced": interlaced, "height": height, "group": group}


class KeyTest(unittest.TestCase):
    def test_one_channel_under_different_spelling(self):
        self.assertEqual(prepare.key("Звезда HD"), prepare.key("Звезда"))
        self.assertEqual(prepare.key("Пятница!"), prepare.key("Пятница"))

    def test_rossiya_k_is_kultura(self):
        self.assertEqual(prepare.key("Россия К"), prepare.key("Культура"))

    def test_epg_key_handles_homoglyphs_and_tv_prefix(self):
        self.assertEqual(epg_ids.key("Dомашний"), epg_ids.key("Домашний"))
        self.assertEqual(epg_ids.key("ТВ-3"), epg_ids.key("ТВ3"))
        self.assertEqual(epg_ids.key("Звезда HD"), epg_ids.key("Звезда"))


class MergeTest(unittest.TestCase):
    def test_first_source_gives_main_link(self):
        main = [channel("СТС", "https://a/sts.m3u8")]
        extra = [channel("СТС HD", "https://b/sts.m3u8")]

        merged = prepare.merge([main, extra])

        self.assertEqual(1, len(merged))
        self.assertEqual(["https://a/sts.m3u8", "https://b/sts.m3u8"], [u for u, *_ in merged[0]["urls"]])

    def test_same_rutube_stream_with_other_signature_is_one_link(self):
        a = "https://bl.rutube.ru/livestream/abc123/index.m3u8?s=one&e=1"
        b = "https://bl.rutube.ru/livestream/abc123/index.m3u8?s=two&e=2"

        merged = prepare.merge([[channel("ТНТ", a)], [channel("ТНТ", b)]])

        self.assertEqual(1, len(merged[0]["urls"]))


class UnwrapMasterTest(unittest.TestCase):
    MASTER = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1,CODECS=\"{codecs}\"\ntracks/mono.m3u8\n"

    def test_single_variant_without_audio_codec_is_unwrapped(self):
        body = self.MASTER.format(codecs="avc1.4d401e").encode()

        self.assertEqual("http://h/ch/tracks/mono.m3u8", prepare.unwrap_master("http://h/ch/video.m3u8", body))

    def test_variant_with_audio_codec_stays(self):
        body = self.MASTER.format(codecs="avc1.4d401e,mp4a.40.2").encode()

        self.assertEqual("http://h/ch/video.m3u8", prepare.unwrap_master("http://h/ch/video.m3u8", body))

    def test_several_variants_stay(self):
        body = (self.MASTER.format(codecs="avc1") + "#EXT-X-STREAM-INF:BANDWIDTH=2\nhd/mono.m3u8\n").encode()

        self.assertEqual("http://h/ch/video.m3u8", prepare.unwrap_master("http://h/ch/video.m3u8", body))


class MeasurementTest(unittest.TestCase):
    RUSSIA = {"ok": True, "hls": True, "interlaced": False, "height": 1080, "ts": 0}

    def test_ci_takes_quality_from_russian_measurement(self):
        # Из-за границы «Витрина» отдаёт заглушку 360p.
        self.assertEqual((False, 1080), prepare.quality((False, 360), self.RUSSIA, ci=True))

    def test_ci_uses_own_measurement_without_russian_one(self):
        self.assertEqual((False, 720), prepare.quality((False, 720), None, ci=True))

    def test_run_from_russia_uses_fresh_measurement(self):
        self.assertEqual((True, 576), prepare.quality((True, 576), self.RUSSIA, ci=False))

    def test_ci_does_not_write_cache(self):
        self.assertFalse(prepare.writes_cache(ci=True))
        self.assertTrue(prepare.writes_cache(ci=False))


class FederalFirstTest(unittest.TestCase):
    def test_federal_channels_go_first_in_fixed_order(self):
        channels = prepare.merge([[
            channel("Мульт", "u1", group="Детские"),
            channel("СТС", "u2"),
            channel("Первый канал HD", "u3"),
        ]])

        ordered = prepare.federal_first(channels)

        self.assertEqual(["Первый канал HD", "СТС", "Мульт"], [c["name"] for c in ordered])
        self.assertEqual(prepare.FEDERAL_GROUP, ordered[0]["group"])


if __name__ == "__main__":
    unittest.main()
