#!/usr/bin/env python3
"""Находит официальные прямые эфиры каналов на RuTube.

Для каждого канала скрипт ищет прямой эфир на RuTube. Скрипт берёт эфир,
только если автор совпадает с названием канала. Так скрипт отбрасывает
ретрансляции посторонних пользователей. Ссылку на поток отдаёт API RuTube.
Срок действия подписи ссылки — несколько лет. Скрипт пишет m3u в stdout.

Запуск: python3 -I tools/rutube.py sources/iptvru.m3u sources/smotret.m3u > sources/rutube.m3u
"""
import concurrent.futures as cf
import json
import re
import subprocess
import sys
from urllib.parse import quote

UA = "Mozilla/5.0 (X11; Linux x86_64)"


def get_json(url):
    r = subprocess.run(["curl", "-sL", "-m", "15", "-A", UA, url], capture_output=True)
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {}


def key(name):
    k = name.lower().replace("ё", "е")
    k = re.sub(r"\b(hd|fhd|uhd|4k|tv|тв|телеканал|канал|официальный|прямой эфир)\b", "", k)
    return re.sub(r"[^\wа-я]+", "", k)


def names(paths):
    """(название, группа) каналов из плейлистов, без повторов."""
    seen = {}
    for path in paths:
        for line in open(path, encoding="utf-8"):
            if line.startswith("#EXTINF"):
                name = line.rsplit(",", 1)[-1].strip()
                group = re.search(r'group-title="([^"]*)"', line)
                seen.setdefault(key(name), (name, group.group(1) if group else ""))
    return [v for k, v in seen.items() if len(k) >= 2]


def find_live(entry):
    name, group = entry
    want = key(name)
    data = get_json("https://rutube.ru/api/search/video/?content_type=live&query="
                    + quote(f"{name} прямой эфир"))
    for item in data.get("results", []):
        author = (item.get("author") or {}).get("name") or ""
        if not item.get("is_livestream") or key(author) != want:
            continue
        options = get_json(f"https://rutube.ru/api/play/options/{item['id']}/?no_404=true")
        hls = (options.get("live_streams") or {}).get("hls") or []
        if hls and hls[0].get("url"):
            return name, group, hls[0]["url"]
    return None


def main():
    with cf.ThreadPoolExecutor(6) as ex:
        found = [r for r in ex.map(find_live, names(sys.argv[1:])) if r]
    print("#EXTM3U")
    for name, group, url in found:
        print(f'#EXTINF:-1 group-title="{group}",{name}')
        print(url)
    print(f"official RuTube lives: {len(found)}: " + ", ".join(n for n, *_ in found), file=sys.stderr)


if __name__ == "__main__":
    main()
