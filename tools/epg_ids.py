#!/usr/bin/env python3
"""Проставляет каналам идентификаторы телепрограммы: Mail.ru (x-mailru) и iptvx (x-epg).

Телепрограмму приложение загружает само по запросу. Источники:
- tv.mail.ru — текущие телесутки; стик открывает этот сайт без ошибок;
- epg.iptvx.one/id/<id> — неделя; часть провайдеров не пропускает этот сайт.

Скрипт только сопоставляет каналы. Для iptvx он сначала берёт tvg-id
из исходного плейлиста, затем сравнивает названия. Для Mail.ru он
сравнивает названия.

Скрипт правит файл channels.m3u. Запускайте его после prepare.py.

Запуск: python3 -I tools/epg_ids.py sources/iptvru.m3u
"""
import html
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAYLIST = os.path.join(ROOT, "channels.m3u")
UA = "Mozilla/5.0"


# латинские буквы, которыми подменяют кириллицу в названиях («Dомашний» у Mail.ru)
HOMOGLYPHS = str.maketrans("aceopxykmtbhd", "асеорхукмтвнд")


# одни и те же каналы под разными названиями
ALIASES = {"россияк": "культура"}


def key(name):
    k = name.lower().replace("ё", "е")
    k = re.sub(r"\b(hd|fhd|uhd|4k|tv|телеканал|канал)\b", "", k)
    k = re.sub(r"\bтв\b-?|\bтв(?=\d)", "", k)  # «ТВ-3» и «ТВ3» — одно и то же
    if re.search(r"[а-я]", k):
        k = k.translate(HOMOGLYPHS)
    k = re.sub(r"[^\wа-я]+", "", k)
    return ALIASES.get(k, k)


def iptvx_channels():
    """{id: название} из списка каналов на любой странице epg.iptvx.one."""
    page = subprocess.run(["curl", "-sL", "-m", "30", "-A", UA, "https://epg.iptvx.one/id/sts"],
                          capture_output=True).stdout.decode("utf-8", "ignore")
    return {i: html.unescape(n).strip()
            for i, n in re.findall(r'<option value="\.\./id/([^"]+)">([^<]+)</option>', page)}


def mailru_channels():
    """{id: название} каналов Mail.ru: обходим категории со всеми страницами."""
    cats = ["central", "children", "education", "general", "hd", "hobbies", "local",
            "movies_series", "music", "news", "other", "sport"]
    found = {}
    for cat in cats:
        for page in range(1, 10):
            url = f"https://tv.mail.ru/moskva/{cat}/" + (f"?page={page}" if page > 1 else "")
            text = subprocess.run(["curl", "-sL", "-m", "20", "-A", UA, url],
                                  capture_output=True).stdout.decode("utf-8", "ignore")
            pairs = re.findall(r'href="/moskva/channel/(\d+)/"[^>]*>\s*(?:<[^>]+>\s*)*([^<]{2,60})<', text)
            if not any(i not in found for i, _ in pairs):
                break
            for i, n in pairs:
                found.setdefault(i, html.unescape(n).strip())
    return found


def tvg_ids(path):
    """{ключ названия: tvg-id} из исходного плейлиста."""
    out = {}
    for line in open(path, encoding="utf-8"):
        if line.startswith("#EXTINF"):
            m = re.search(r'tvg-id="([^"]+)"', line)
            if m:
                out.setdefault(key(line.rsplit(",", 1)[-1]), m.group(1))
    return out


def main():
    ids = iptvx_channels()
    mail = {}
    for i, n in mailru_channels().items():
        mail.setdefault(key(n), i)
    # без региональных сдвигов (+2, +4…) и пометок вроде [DE]
    by_name = {}
    for i, n in ids.items():
        if not re.search(r"\(\+\d+\)|\[[A-Z]{2}]", n):
            by_name.setdefault(key(n), i)
    tvg = {}
    for src in sys.argv[1:]:
        tvg.update({k: v for k, v in tvg_ids(src).items() if k not in tvg})

    lines = open(PLAYLIST, encoding="utf-8").read().splitlines()
    matched = mail_matched = total = 0
    for n, line in enumerate(lines):
        if not line.startswith("#EXTINF"):
            continue
        total += 1
        line = re.sub(r' x-(epg|mailru)="[^"]*"', "", line)
        k = key(line.rsplit(",", 1)[-1])
        mailru = mail.get(k) or mail.get(re.sub(r"(россия|russia|рус)$", "", k))
        if mailru:
            mail_matched += 1
            head, _, name = line.rpartition(",")
            line = f'{head} x-mailru="{mailru}",{name}'
        epg = tvg.get(k) if tvg.get(k) in ids else by_name.get(k)
        if not epg:
            # «Nickelodeon Россия» в iptvx просто «Nickelodeon»
            epg = by_name.get(re.sub(r"(россия|russia|рус)$", "", k))
        if epg:
            matched += 1
            head, _, name = line.rpartition(",")
            line = f'{head} x-epg="{epg}",{name}'
        lines[n] = line
    with open(PLAYLIST, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"iptvx channels: {len(ids)}, matched: {matched}/{total}; "
          f"mail.ru channels: {len(mail)}, matched: {mail_matched}/{total}")


if __name__ == "__main__":
    main()
