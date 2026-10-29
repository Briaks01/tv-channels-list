#!/usr/bin/env python3
"""Собирает список каналов из одного или нескольких m3u-плейлистов.

Скрипт выполняет такие действия:
- Проверяет каждый поток и отбрасывает нерабочие.
- Склеивает один канал из разных источников. Лишние ссылки скрипт делает запасными.
- Ставит федеральные каналы первыми.
- Приводит категории к одному списку.
- Скачивает логотипы в каталог logos/.
- Записывает файл channels.m3u.

Формат channels.m3u: под строкой #EXTINF стоят одна или несколько ссылок.
Первая ссылка основная, остальные запасные. Атрибут x-hls="1" указывает поток HLS.

Порядок файлов задаёт приоритет источников. Поэтому smotret.tv передают последним.

Качество важнее источника. Ссылки канала сортируют так:

1. Прогрессивное видео идёт раньше чересстрочного. Стик чересстрочный SD почти не воспроизводит.
2. Большее разрешение идёт раньше.
3. При равном разрешении порядок задают файлы.

Запуск: python3 -I tools/prepare.py [--ci] основной.m3u [дополнительный.m3u ...]
"""
import concurrent.futures as cf
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urljoin

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Список каналов и логотипы лежат в корне репозитория. Приложение скачивает их отсюда.
ASSETS = ROOT
LOGOS = os.path.join(ROOT, "logos")
UA = "Mozilla/5.0"

# Первая двадцатка с привычными номерами 1–20, как у Триколора.
# Если вариантов несколько, скрипт берёт первый шаблон, который совпал.
FEDERAL_GROUP = "Федеральные"
FEDERAL = [
    [r"Первый канал HD", r"Первый канал"],
    [r"Россия 1 HD", r"Россия 1"],
    [r"Матч!?"],
    [r"НТВ HD", r"НТВ"],
    [r"Пятый канал"],
    [r"Россия К"],
    [r"Россия 24"],
    [r"Карусель"],
    [r"ОТР"],
    [r"ТВ Центр"],
    [r"Р[еЕ][нН] ТВ"],
    [r"Спас"],
    [r"СТС"],
    [r"Домашний"],
    [r"ТВ-?3"],
    [r"Пятница!?"],
    [r"Звезда HD", r"Звезда"],
    [r"Мир"],
    [r"ТНТ HD", r"ТНТ"],
    [r"Муз[- ]ТВ"],
]

# Единый список категорий: исходное название -> название в приложении
GROUPS = {
    "Общественные": "Общественные",
    "Информационные": "Новости",
    "Детские": "Детские",
    "Кино и сериалы": "Кино и сериалы",
    "Кино и сериалы (Российские)": "Кино и сериалы",
    "Кино Online": "Кино и сериалы",
    "Развлекательные": "Развлекательные",
    "Спортивные": "Спорт",
    "Познавательные": "Познавательные",
    "Образовательные": "Познавательные",
    "Культура": "Познавательные",
    "Хобби и увлечения": "Хобби",
    "Музыкальные": "Музыка",
    "Релакс": "Музыка",
    "Христианские": "Религиозные",
    "Местные": "Региональные",
    "Развлекательные (Местные)": "Региональные",
}
# Порядок вкладок в приложении (остальное — в конце по алфавиту)
GROUP_ORDER = [FEDERAL_GROUP, "Детские", "Кино и сериалы", "Развлекательные", "Спорт",
               "Новости", "Познавательные", "Музыка", "Хобби", "Общественные",
               "Религиозные", "Региональные"]


def parse(path):
    lines = open(path, encoding="utf-8").read().splitlines()
    items = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXTINF"):
            continue
        url = next((x.strip() for x in lines[i + 1:i + 5]
                    if x.strip() and not x.startswith("#")), None)
        if not url:
            continue
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', line))
        name = line.rsplit(",", 1)[-1].strip()
        items.append({"name": name, "url": url, "attrs": attrs})
    return items


# Один канал под разными названиями в разных источниках.
ALIASES = {"россияк": "культура"}


def key(name):
    """Возвращает ключ для склейки одного канала из разных источников."""
    k = name.lower().replace("ё", "е")
    k = re.sub(r"\b(hd|fhd|uhd|4k|tv|тв)\b", "", k)
    k = re.sub(r"[^\wа-я]+", "", k)
    return ALIASES.get(k, k)


AUDIO_CODECS = re.compile(r"mp4a|ac-3|ec-3|mp3|opus|\.6b|\.69", re.I)


def unwrap_master(url, body):
    """Заменяет мастер-плейлист ссылкой на вариант потока.

    Условие замены: вариант один, и атрибут CODECS не указывает звук.
    ExoPlayer читает такой плейлист как поток без звука. Аппаратный
    декодер тогда не выдаёт кадров."""
    text = body.decode("utf-8", "ignore")
    variants = re.findall(r"#EXT-X-STREAM-INF:([^\n]*)\n([^\n#][^\n]*)", text)
    if len(variants) != 1:
        return url
    attrs, variant = variants[0]
    codecs = re.search(r'CODECS="([^"]*)"', attrs)
    if not codecs or AUDIO_CODECS.search(codecs.group(1)):
        return url
    return urljoin(url, variant.strip())


def okru_stream(video_id):
    """Ссылка на HLS трансляции OK.ru (живёт сутки, поэтому в плейлист
    пишем okru:<id>, а приложение достаёт поток так же само)."""
    r = subprocess.run(["curl", "-sL", "-m", "15", "-A", UA,
                        f"https://ok.ru/videoembed/{video_id}"], capture_output=True)
    m = re.search(r'data-options="([^"]+)"', r.stdout.decode("utf-8", "ignore"))
    if not m:
        return None
    try:
        meta = json.loads(html.unescape(m.group(1)))["flashvars"]["metadata"]
        meta = json.loads(meta) if isinstance(meta, str) else meta
    except (ValueError, KeyError):
        return None
    return meta.get("hlsMasterPlaylistUrl") or meta.get("hlsManifestUrl") or meta.get("ondemandHls")


def vitrina_stream(player_url):
    """Поток «Витрины ТВ»: шаблон балансировщика берётся из плеера канала."""
    # плеер отдаётся сжатым даже без запроса сжатия — нужен --compressed
    r = subprocess.run(["curl", "-sL", "--compressed", "-m", "15", "-A", UA,
                        "-e", "https://smotret.tv/", player_url], capture_output=True)
    m = re.search(r"https://media\.mediavitrina\.ru/balancer/v3/[^\"'\s]+",
                  r.stdout.decode("utf-8", "ignore"))
    if not m:
        return None
    balancer = m.group(0).replace("{{PLAYER_REFERER_HOSTNAME}}", "smotret.tv")
    balancer = re.sub(r"\{\{\w+}}", "", balancer)
    # балансировщик отвечает только плееру «Витрины» (по Referer)
    r = subprocess.run(["curl", "-sL", "--compressed", "-m", "15", "-A", UA, "-e", player_url,
                        balancer], capture_output=True)
    try:
        hls = json.loads(r.stdout).get("hls") or []
    except ValueError:
        return None
    return hls[0] if hls else None


def vkplay_stream(channel):
    """Поток VK Play Live (live.vkvideo.ru) через открытый API канала."""
    r = subprocess.run(["curl", "-sL", "--compressed", "-m", "15", "-A", UA,
                        f"https://api.live.vkvideo.ru/v1/blog/{channel}/public_video_stream"],
                       capture_output=True)
    try:
        stream = (json.loads(r.stdout).get("data") or [{}])[0]
    except ValueError:
        return None
    urls = {}
    for key in ("playerUrls", "sharedPlayerUrls"):
        for p in stream.get(key) or []:
            if p.get("url"):
                urls.setdefault(p.get("type"), p["url"])
    return urls.get("live_hls") or urls.get("live_playback_hls")


def resolve(url):
    """okru:/vitrina:/vkplay: -> настоящая ссылка на поток (или None)."""
    if url.startswith("okru:"):
        return okru_stream(url[5:])
    if url.startswith("vitrina:"):
        return vitrina_stream(url[8:])
    if url.startswith("vkplay:"):
        return vkplay_stream(url[7:])
    return url


def probe(url):
    """Возвращает кортеж: поток работает, поток HLS, ссылка. Ссылку на мастер-плейлист скрипт может заменить ссылкой на вариант."""
    if url.startswith(("okru:", "vitrina:", "vkplay:")):
        stream = resolve(url)
        return (stream is not None and probe(stream)[0]), True, url
    r = subprocess.run(["curl", "-sL", "-m", "10", "-A", UA, "-r", "0-65535",
                        "-o", "-", "-w", "\n%{http_code} %{url_effective}", url],
                       capture_output=True)
    body, _, tail = r.stdout.rpartition(b"\n")
    code, _, effective = tail.decode().partition(" ")
    if code not in ("200", "206"):
        return False, False, url
    hls = b"#EXTM3U" in body[:300]
    if hls:
        unwrapped = unwrap_master(effective, body)
        if unwrapped != effective:
            print("UNWRAP", url, "->", unwrapped)
            return True, True, unwrapped
    return hls or len(body) > 20000, hls, url


def logo_key(ch):
    k = ch["attrs"].get("tvg-id") or ch["name"]
    if re.fullmatch(r"[\w.-]+", k, re.ASCII):
        return k
    return hashlib.md5(k.encode()).hexdigest()[:12]


def fetch_logo(ch):
    url = ch["attrs"].get("tvg-logo")
    if not url:
        return None
    name = logo_key(ch)
    dst = os.path.join(LOGOS, name + ".png")
    if os.path.exists(dst):
        return name
    with tempfile.NamedTemporaryFile(suffix=".img") as tmp:
        r = subprocess.run(["curl", "-sfL", "-m", "15", "-A", UA, "-o", tmp.name, url])
        if r.returncode != 0:
            return None
        # уменьшаем до высоты 96 px — для сетки на ТВ больше не нужно
        r = subprocess.run(["magick", tmp.name, "-resize", "x96>", "-strip", dst],
                           capture_output=True)
    return name if r.returncode == 0 else None


def inspect(url):
    """(чересстрочный, высота кадра) лучшего варианта потока.

    Чересстрочное видео (эфирный SD) на стике почти не играет: декодеры
    Android его не тянут, аппаратный Amlogic — через раз. Высота нужна,
    чтобы первым ставить поток в лучшем качестве."""
    url = resolve(url)
    if url is None:
        return False, 0
    r = subprocess.run(["curl", "-sL", "-m", "10", "-A", UA, "-r", "0-65535", "-o", "-",
                        "-w", "\n%{url_effective}", url], capture_output=True)
    body, _, effective = r.stdout.rpartition(b"\n")
    text = body.decode("utf-8", "ignore")
    height = 0
    variants = re.findall(r"#EXT-X-STREAM-INF:([^\n]*)\n([^\n#][^\n]*)", text)
    if variants:
        # мастер-плейлист: высоту берём из RESOLUTION, кадры смотрим у лучшего варианта
        def h(attrs):
            m = re.search(r"RESOLUTION=\d+x(\d+)", attrs)
            return int(m.group(1)) if m else 0
        attrs, best = max(variants, key=lambda v: h(v[0]))
        height = h(attrs)
        url = urljoin(effective.decode(), best.strip())
    r = subprocess.run(["ffprobe", "-v", "error", "-rw_timeout", "10000000",
                        "-user_agent", UA, "-select_streams", "v:0", "-show_frames",
                        "-read_intervals", "%+#2", "-show_entries", "frame=height,interlaced_frame",
                        "-of", "csv=p=0", url], capture_output=True, timeout=40)
    # ffprobe печатает поля в своём порядке: «высота,чересстрочность»
    frames = re.findall(r"^(\d+),([01])", r.stdout.decode("utf-8", "ignore"), re.M)
    interlaced = any(flag == "1" for _, flag in frames)
    if not height:
        height = max((int(h) for h, _ in frames), default=0)
    return interlaced, height


def stream_id(url):
    """Одна трансляция RuTube приходит из разных источников с разной подписью."""
    m = re.match(r"https://bl\.rutube\.ru/livestream/(\w+)/", url)
    return m.group(1) if m else url


def merge(sources):
    """Склеивает каналы по названию; порядок — по первому появлению."""
    merged = {}
    for items in sources:
        for ch in items:
            k = key(ch["name"])
            if k not in merged:
                ch["urls"] = [(ch["url"], ch["hls"], ch["interlaced"], ch["height"])]
                merged[k] = ch
                continue
            base = merged[k]
            if all(stream_id(u) != stream_id(ch["url"]) for u, *_ in base["urls"]):
                base["urls"].append((ch["url"], ch["hls"], ch["interlaced"], ch["height"]))
            if not base["attrs"].get("tvg-logo") and ch["attrs"].get("tvg-logo"):
                base["attrs"]["tvg-logo"] = ch["attrs"]["tvg-logo"]
    return list(merged.values())


def federal_first(channels):
    """Выносит федеральные каналы в начало в порядке FEDERAL."""
    rest = list(channels)
    head = []
    for variants in FEDERAL:
        found = None
        for pattern in variants:
            found = next((c for c in rest if re.fullmatch(pattern, c["name"])), None)
            if found:
                break
        if found:
            rest.remove(found)
            found["group"] = FEDERAL_GROUP
            head.append(found)
        else:
            print("NO FEDERAL", variants[0])
    rank = {g: i for i, g in enumerate(GROUP_ORDER)}
    rest.sort(key=lambda c: (rank.get(c["group"], len(rank)), c["group"]))
    return head + rest


def safe(fn, *args):
    try:
        return fn(*args)
    except subprocess.TimeoutExpired:
        return False, 0


CACHE_PATH = os.path.join(ROOT, "sources", "probe_cache.json")
CACHE_FRESH_DAYS = 14
CACHE_KEEP_DAYS = 60
# ссылки, поток по которым приложение достаёт само на стике (или официальный RuTube):
# из-за границы они могут не открываться, но это не значит, что они мёртвые
RUNTIME = ("okru:", "vitrina:", "vkplay:", "https://bl.rutube.ru/")


def load_cache():
    try:
        return json.load(open(CACHE_PATH, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cache(cache, now):
    keep = {k: v for k, v in cache.items() if now - v.get("ts", 0) < CACHE_KEEP_DAYS * 86400}
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(keep, f, ensure_ascii=False, indent=0, sort_keys=True)


def main():
    # Ключ --ci включают в GitHub Actions. Сервер стоит за границей.
    # Часть источников отдаёт потоки только в Россию. Скрипт сохраняет поток в двух случаях:
    # - Поток работал из России в последние 14 дней. Это видно в sources/probe_cache.json.
    # - Ссылку на поток приложение получает на стике.
    ci = "--ci" in sys.argv
    sources = [parse(p) for p in sys.argv[1:] if not p.startswith("--")]
    flat = [ch for items in sources for ch in items]
    cache, now = load_cache(), time.time()
    with cf.ThreadPoolExecutor(24) as ex:
        probes = list(ex.map(lambda c: probe(c["url"]), flat))
    for ch, (ok, hls, url) in zip(flat, probes):
        ch["key"] = stream_id(ch["url"])
        ch["measured"] = ok
        cached = cache.get(ch["key"])
        if not ok and ci:
            if cached and cached.get("ok") and now - cached["ts"] < CACHE_FRESH_DAYS * 86400:
                ok, hls, url = True, cached["hls"], cached.get("url", ch["url"])
            elif ch["url"].startswith(RUNTIME):
                ok, hls = True, True
        ch["ok"], ch["hls"], ch["url"] = ok, hls, url
        ch["group"] = GROUPS.get(ch["attrs"].get("group-title"), ch["attrs"].get("group-title", "Другие"))
    dead = sorted({c["name"] for c in flat if not c["ok"]})
    alive = [c for c in flat if c["ok"]]
    measured = [c for c in alive if c["measured"]]
    with cf.ThreadPoolExecutor(16) as ex:
        info = list(ex.map(lambda c: safe(inspect, c["url"]) if c["hls"] else (False, 0), measured))
    for ch, (inter, height) in zip(measured, info):
        ch["interlaced"], ch["height"] = inter, height
    for ch in alive:
        if not ch["measured"]:
            cached = cache.get(ch["key"], {})
            ch["interlaced"], ch["height"] = cached.get("interlaced", False), cached.get("height", 0)
    # кэш замеров: из России пишем всё, из CI — только то, что реально открылось
    for ch in flat:
        if ch["measured"]:
            cache[ch["key"]] = {"ok": True, "hls": ch["hls"], "url": ch["url"],
                                "interlaced": ch.get("interlaced", False),
                                "height": ch.get("height", 0), "ts": int(now)}
        elif not ci:
            cache[ch["key"]] = {"ok": False, "ts": int(now)}
    save_cache(cache, now)
    flags = [ch["interlaced"] for ch in alive]
    channels = merge([[c for c in items if c["ok"]] for items in sources])
    # сначала прогрессивные, среди них — лучшее разрешение; при равных — приоритет источника
    demoted = 0
    for ch in channels:
        ordered = sorted(ch["urls"], key=lambda u: (u[2], -u[3]))
        if ordered[0] != ch["urls"][0]:
            demoted += 1
        ch["urls"] = ordered
    channels = federal_first(channels)

    os.makedirs(LOGOS, exist_ok=True)
    with cf.ThreadPoolExecutor(16) as ex:
        logos = list(ex.map(fetch_logo, channels))
    out = ["#EXTM3U"]
    for ch, logo in zip(channels, logos):
        attrs = f'group-title="{ch["group"]}"'
        if logo:
            attrs += f' x-logo="{logo}"'
        if ch["urls"][0][1]:
            attrs += ' x-hls="1"'
        out.append(f"#EXTINF:-1 {attrs},{ch['name']}")
        out += [u for u, *_ in ch["urls"]]
    with open(os.path.join(ASSETS, "channels.m3u"), "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")

    used = {f"{x}.png" for x in logos if x}
    for f in os.listdir(LOGOS):
        if f not in used:
            os.remove(os.path.join(LOGOS, f))
    groups = {}
    for ch in channels:
        groups[ch["group"]] = groups.get(ch["group"], 0) + 1
    print(f"dead streams: {len(dead)}, interlaced: {sum(flags)}, "
          f"channels where first stream changed by quality: {demoted}")
    best = [max((u[3] for u in c["urls"] if not u[2]), default=0) for c in channels]
    print("best quality: " + ", ".join(f"{q}: {sum(1 for b in best if lo <= b < hi)}" for q, lo, hi in
          [("1080p+", 1000, 10**5), ("720p", 700, 1000), ("SD", 1, 700), ("unknown", 0, 1)]))
    print(f"channels: {len(channels)}, with backup: {sum(len(c['urls']) > 1 for c in channels)}, "
          f"logos: {len(used)}")
    print(", ".join(f"{g} {n}" for g, n in groups.items()))


if __name__ == "__main__":
    main()
