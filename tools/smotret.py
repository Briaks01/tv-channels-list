#!/usr/bin/env python3
"""Собирает каналы с smotret.tv в m3u.

Скрипт обходит категории сайта и каналы с главной страницы.
На странице канала скрипт находит плеер (iframe) и берёт из него ссылки на HLS.

Сайт встраивает плееры других сервисов:
- RuTube. Ссылку на поток скрипт получает через API RuTube.
- OK.ru. Скрипт пишет ссылку okru:<id>.
- «Витрина ТВ». Скрипт пишет ссылку vitrina:<адрес плеера>.
- VK Видео. Без входа в аккаунт он поток не отдаёт. Скрипт пропускает такой канал.

Ссылки okru: и vitrina: приложение превращает в поток само: токены быстро истекают.
Запасные ссылки идут отдельными записями. Скрипт prepare.py склеивает их в один канал.

Запуск: python3 -I tools/smotret.py > smotret.m3u
"""
import concurrent.futures as cf
import html
import json
import re
import subprocess
import sys

BASE = "https://smotret.tv"
UA = "Mozilla/5.0 (X11; Linux x86_64)"

# категория сайта -> группа в приложении
CATEGORIES = {
    "detskie": "Детские",
    "music": "Музыкальные",
    "sport": "Спортивные",
    "news": "Информационные",
    "razvlekatelnye": "Развлекательные",
    "poznavatelnye": "Познавательные",
    "christian": "Христианские",
    "relax": "Релакс",
}
# каналы с главной (без категории в адресе) — группа по умолчанию;
# если канал уже есть в основном плейлисте, prepare.py возьмёт группу оттуда
ROOT_GROUP = "Развлекательные"
# радио и прочее без видео
SKIP = re.compile(r"radio|-fm$|^fm-", re.I)


def get(path):
    url = path if path.startswith("http") else BASE + path
    r = subprocess.run(["curl", "-sL", "-m", "15", "-A", UA, "-e", BASE + "/", url],
                       capture_output=True)
    return r.stdout.decode("utf-8", "ignore")


def rutube(video_id):
    raw = get(f"https://rutube.ru/api/play/options/{video_id}/?no_404=true"
              f"&referer=https%3A%2F%2Fsmotret.tv%2F")
    try:
        hls = json.loads(raw).get("live_streams", {}).get("hls", [])
    except ValueError:
        return []
    return [h["url"] for h in hls if h.get("url")]


def player_urls(src):
    """Возвращает ссылки на поток из плеера на странице (iframe). Плеер бывает свой или чужой."""
    if m := re.search(r"rutube\.ru/play/embed/(\w+)", src):
        return rutube(m.group(1))
    if m := re.search(r"ok\.ru/videoembed/(\d+)", src):
        return [f"okru:{m.group(1)}"]
    if "player.mediavitrina.ru" in src:
        return [f"vitrina:{src.split('?')[0]}"]
    player = get(src)
    nested = re.search(r'<iframe[^>]+src="([^"]+)"', player)
    if nested and not src.startswith("http") and "smotret.tv" not in nested.group(1):
        return player_urls(nested.group(1))
    return list(dict.fromkeys(re.findall(r'https?://[^"\'\s<>]+\.m3u8[^"\'\s<>]*', player)))


def channel(path, group):
    page = get(path)
    m = re.search(r'<iframe[^>]+src="([^"]+)"', page)
    if not m:
        return []
    urls = player_urls(m.group(1))
    title = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    name = html.unescape(re.sub(r"<[^>]+>", "", title.group(1))).strip() if title else path
    name = re.sub(r"\s*(смотреть|онлайн|прямой эфир).*$", "", name, flags=re.I)
    name = re.split(r"\s+[—–-]\s*", name)[0].strip()
    slug = path.rstrip("/").rsplit("/", 1)[-1]
    logo = f"{BASE}/images/{slug}.webp"
    return [(name, group, logo, u) for u in urls]


def main():
    jobs = []
    for cat, group in CATEGORIES.items():
        page = get(f"/{cat}/")
        paths = dict.fromkeys(re.findall(rf'href="(/{cat}/[\w-]+)"', page))
        jobs += [(p, group) for p in paths if not SKIP.search(p.rsplit("/", 1)[-1])]
    home = get("/")
    known = {p for p, _ in jobs}
    for p in dict.fromkeys(re.findall(r'href="(/[\w-]+)"', home)):
        if p.strip("/") not in CATEGORIES and p not in known and not SKIP.search(p):
            jobs.append((p, ROOT_GROUP))
    with cf.ThreadPoolExecutor(12) as ex:
        results = list(ex.map(lambda j: channel(*j), jobs))
    print("#EXTM3U")
    for entries in results:
        for name, group, logo, url in entries:
            print(f'#EXTINF:-1 group-title="{group}" tvg-logo="{logo}",{name}')
            print(url)
    print(f"pages: {len(jobs)}, with streams: {sum(1 for r in results if r)}", file=sys.stderr)


if __name__ == "__main__":
    main()
