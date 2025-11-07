#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RSS парсер для подкаста «Волна с Востока»
Генерирует episodes.json для поиска на сайте
"""

import argparse
import feedparser
import json
import os
import re
import sys
from datetime import datetime, timezone
from html import unescape
from email.utils import parsedate_to_datetime

DEFAULT_RSS = os.getenv("RSS_URL")
# Проверяем что RSS_URL установлен
if not DEFAULT_RSS:
    print("❌ ОШИБКА: Переменная окружения RSS_URL не установлена!")
    print("   Установите её в GitHub Secrets или в .env файле")
    sys.exit(1)

DEFAULT_OUT = "episodes.json"
EXTRAS_FILE = os.getenv("EXTRAS_FILE", "extras_map.json")

def clean_html(text: str) -> str:
    """Грубое удаление HTML-тегов + unescape, схлопывание пробелов."""
    if not text:
        return ""
    text = unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    # Если ссылки в описании не нужны для сайта – убираем:
    text = re.sub(r"https?://\S+", "", text)
    return text.strip()


def parse_duration(raw) -> str:
    """Нормализация длительности к H:MM:SS или M:SS."""
    if not raw:
        return ""
    s = str(raw).strip()

    # Если уже формата H:MM:SS или M:SS — оставляем
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", s):
        # Приведём к H:MM:SS при возможности
        parts = list(map(int, s.split(":")))
        if len(parts) == 2:
            m, sec = parts
            return f"{m}:{sec:02d}"
        h, m, sec = (parts + [0, 0])[:3]
        return f"{h}:{m:02d}:{sec:02d}"

    # Варианты "3723", "1h02m03s", "95m12s"
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", s.lower())
    if m and any(m.groups()):
        h = int(m.group(1) or 0)
        mi = int(m.group(2) or 0)
        sec = int(m.group(3) or 0)
        if h > 0:
            return f"{h}:{mi:02d}:{sec:02d}"
        return f"{mi}:{sec:02d}"

    try:
        total = int(s)
        h = total // 3600
        mi = (total % 3600) // 60
        sec = total % 60
        if h > 0:
            return f"{h}:{mi:02d}:{sec:02d}"
        return f"{mi}:{sec:02d}"
    except (ValueError, TypeError):
        return s
        
def load_extras_map(path: str) -> dict:
    """Загружает карту доп.полей по номеру эпизода."""
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            print(f"⚠️ Не удалось прочитать {path}: {e}")
    return {}

def norm_epnum(v) -> str:
    """Нормализует episode_number к строке ('8', '12'...), чтобы ключи совпадали с JSON-картой."""
    if v is None or v == "":
        return ""
    try:
        return str(int(v))
    except Exception:
        # если вдруг в RSS строка, сохраняем как есть (например, 'S1E8')
        return str(v).strip()


def coerce_datetime(entry) -> tuple[datetime | None, str, int | None]:
    """Достаём дату: published/updated (parsed -> datetime), строку для вывода и год."""
    dt = None

    # Parsed поля
    for k in ("published_parsed", "updated_parsed", "created_parsed"):
        if getattr(entry, k, None):
            try:
                dt = datetime(*getattr(entry, k)[:6], tzinfo=timezone.utc)
                break
            except Exception:
                pass

    # Строковые поля
    if dt is None:
        for k in ("published", "updated", "created"):
            v = entry.get(k)
            if v:
                try:
                    dt = parsedate_to_datetime(v)
                    # Приведём naive к UTC
                    if dt and dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    break
                except Exception:
                    continue

    # Формат для вывода
    date_str = dt.astimezone(timezone.utc).strftime("%d.%m.%Y") if dt else ""
    year = dt.year if dt else None
    return dt, date_str, year


def pick_audio(entry) -> str:
    """Ищем аудио сначала в enclosures, потом в links[rel=enclosure]."""
    # enclosures
    for enc in entry.get("enclosures", []):
        t = (enc.get("type") or "").lower()
        if "audio" in t or t in {"audio/mpeg", "audio/mp3", "audio/aac"}:
            return enc.get("href") or ""

    # links rel=enclosure
    for ln in entry.get("links", []):
        if ln.get("rel") == "enclosure":
            t = (ln.get("type") or "").lower()
            if not t or "audio" in t or t in {"audio/mpeg", "audio/mp3", "audio/aac"}:
                return ln.get("href") or ""

    return ""


def pick_image(entry) -> str:
    """itunes:image / media:thumbnail / media:content — первая подходящая."""
    itunes_image = entry.get("itunes_image")
    if isinstance(itunes_image, dict) and itunes_image.get("href"):
        return itunes_image["href"]

    # feedparser может раскладывать media_* по-разному
    for k in ("media_thumbnail", "media_content"):
        arr = entry.get(k) or []
        if isinstance(arr, list) and arr:
            href = arr[0].get("url") or arr[0].get("href")
            if href:
                return href
    return ""


def to_int_or_str(v):
    try:
        return int(v)
    except Exception:
        return str(v) if v is not None else ""


def parse_rss_to_json(rss_url: str, out_path: str) -> int:
    print(f"Загружаю RSS: {rss_url}")
    feed = feedparser.parse(rss_url)
    extras_map = load_extras_map(EXTRAS_FILE)

    if getattr(feed, "bozo", False):
        print(f"⚠️ Предупреждение: {getattr(feed, 'bozo_exception', 'unknown parse issue')}")

    episodes = []
    for idx, entry in enumerate(feed.entries or [], 1):
        try:
            title = entry.get("title", "Без названия").strip()
            # описание: content -> summary_detail -> summary/description
            description = (
                (entry.get("content") or [{}])[0].get("value")
                or (entry.get("summary_detail") or {}).get("value")
                or entry.get("summary")
                or entry.get("description")
                or ""
            )
            description = clean_html(description)
            link = entry.get("link", "").strip()

            pub_dt, date_str, year = coerce_datetime(entry)
            audio_url = pick_audio(entry)
            image_url = pick_image(entry)

            duration = parse_duration(
                entry.get("itunes_duration")
                or entry.get("itunes:duration")
                or entry.get("duration")
            )
            episode_number = to_int_or_str(entry.get("itunes_episode") or entry.get("episode"))
            season = to_int_or_str(entry.get("itunes_season") or entry.get("season"))
            episode_type = entry.get("itunes_episodetype") or entry.get("episodeType") or ""

            guid = entry.get("guid") or entry.get("id") or ""
            explicit = str(entry.get("itunes_explicit") or "").lower() in {"yes", "true", "1"}

            num_key = norm_epnum(episode_number)
            extra = extras_map.get(num_key, {})  # ← вот её и не хватало

            episodes.append(
                {
                    "name": title,
                    "desc": description,
                    "link": link,
                    "audio_url": audio_url,
                    "image": image_url,
                    "date": date_str,
                    "year": year,
                    "duration": duration,
                    "episode_number": episode_number,
                    "season": season,
                    "episode_type": episode_type,
                    "guid": guid,
                    "explicit": explicit,
                    "page": extra.get("page", ""),
                    # «сырая» дата для сортировки/отладки (ISO, UTC)
                    "pub_iso": pub_dt.astimezone(timezone.utc).isoformat() if pub_dt else "",
                }
            )
        except Exception as e:
            print(f"❌ Ошибка в записи {idx}: {e}")
            continue

    # сортировка по реальной дате, затем по имени как стабильный fallback
    episodes.sort(
        key=lambda x: (x["pub_iso"] or "", x["name"]),
        reverse=True,
    )

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(episodes, f, ensure_ascii=False, indent=2)

    print(f"✅ Сохранено {len(episodes)} выпусков → {out_path}")
    if episodes:
        latest = episodes[0]
        print(f"🎙 {latest['name']}  📅 {latest['date']}")

    # нулевое количество — сигнализируем ошибкой возврата
    return len(episodes)


def main():
    ap = argparse.ArgumentParser(description="Parse podcast RSS to JSON")
    ap.add_argument("--rss", default=DEFAULT_RSS, help="RSS URL (env RSS_URL by default)")
    ap.add_argument("--out", default=DEFAULT_OUT, help="Output JSON file path")
    args = ap.parse_args()

    count = parse_rss_to_json(args.rss, args.out)
    sys.exit(0 if count > 0 else 1)


if __name__ == "__main__":
    main()
