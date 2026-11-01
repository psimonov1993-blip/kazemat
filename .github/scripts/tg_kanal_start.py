#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Стартовые посты канала Каземата: 5 постов с картинками, опросами и викторинами. Только стандартная библиотека

Режимы:
  check     ничего не публикует: проверяет бота, канал, права, картинки и длину текстов
  publish   публикует посты по порядку, после каждого его опрос, потом закрепляет пост 1

Тексты и картинки - kanal_start.json рядом со скриптом (собирается из kanal.html)
Что уже отправлено, записано в kanal-start.json в корне сайта: повторный запуск продолжит с места обрыва, а не задвоит
Секреты из окружения: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MSK = datetime.timezone(datetime.timedelta(hours=3))
PAUSE = 2.0  # между сообщениями, чтобы не упереться в лимит канала
NEWS_KEEP = 30


def log(*parts):
    print(*parts, flush=True)


def now():
    return datetime.datetime.now(MSK)


def tg_len(text):
    # Telegram считает длину в единицах UTF-16: эмодзи весит 2
    return len(text.encode("utf-16-le")) // 2


class TgError(Exception):
    pass


class Telegram:
    def __init__(self, token, chat_id):
        self.token, self.chat_id = token, chat_id

    def call(self, method, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = "https://api.telegram.org/bot%s/%s" % (self.token, method)
        for attempt in range(3):
            req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    res = json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as err:
                try:
                    res = json.loads(err.read().decode("utf-8"))
                except Exception:
                    res = {"ok": False, "description": "HTTP %s" % err.code}
                retry = (res.get("parameters") or {}).get("retry_after")
                if err.code == 429 and retry and attempt < 2:
                    log("Telegram просит подождать %s с" % retry)
                    time.sleep(int(retry) + 1)
                    continue
            except urllib.error.URLError as err:
                if attempt < 2:
                    time.sleep(3)
                    continue
                raise TgError("сеть: %s" % err.reason)
            if res.get("ok"):
                return res["result"]
            raise TgError(res.get("description") or "неизвестная ошибка")
        raise TgError("не удалось после трёх попыток")


def head_ok(url):
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "kazemat-tg"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, int(r.headers.get("Content-Length") or 0)
    except urllib.error.HTTPError as err:
        return err.code, 0
    except urllib.error.URLError as err:
        return str(err.reason), 0


def load_state(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"channel": "", "posts": {}}


def save_state(path, state):
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def record_news(root, channel, sent):
    path = root / "news.json"
    try:
        news = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        news = {"channel": "", "posts": []}
    posts = news.get("posts") or []
    if channel and news.get("channel") and news["channel"] != channel:
        posts = []
    known = {p.get("id") for p in posts}
    posts.extend(s for s in sent if s["id"] not in known)
    news = {"channel": channel or news.get("channel") or "", "posts": posts[-NEWS_KEEP:]}
    path.write_text(json.dumps(news, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log("news.json: дописано постов %d" % len(sent))


def caption_for(post, has_comments):
    if not has_comments and post.get("caption_no_comments"):
        return post["caption_no_comments"]
    return post["caption"]


def poll_payload(chat, poll):
    p = {
        "chat_id": chat,
        "question": poll["question"],
        "options": [{"text": o} for o in poll["options"]],
        "is_anonymous": True,
        "disable_notification": True,
    }
    if poll.get("quiz"):
        p["type"] = "quiz"
        p["correct_option_id"] = poll["correct"]
        if poll.get("explanation"):
            p["explanation"] = poll["explanation"]
    else:
        p["type"] = "regular"
        p["allows_multiple_answers"] = bool(poll.get("multiple"))
    return p


def channel_info(tg):
    me = tg.call("getMe", {})
    chat = tg.call("getChat", {"chat_id": tg.chat_id})
    member = tg.call("getChatMember", {"chat_id": tg.chat_id, "user_id": me["id"]})
    return me, chat, member


def check(tg, data):
    problems = 0
    me, chat, member = channel_info(tg)
    log("Бот: @%s" % me.get("username"))
    log("Канал: %s (@%s), id %s" % (chat.get("title"), chat.get("username"), chat.get("id")))
    log("Описание канала: %s" % ((chat.get("description") or "нет").replace("\n", " / ")))
    log("Аватарка канала: %s" % ("есть" if chat.get("photo") else "нет"))
    log("Группа обсуждения (комментарии): %s" % (chat.get("linked_chat_id") or "нет"))
    log("Права бота: статус %s, публикация %s, правка и закреп %s, профиль канала %s" % (
        member.get("status"), member.get("can_post_messages"), member.get("can_edit_messages"), member.get("can_change_info")))
    if member.get("status") not in ("administrator", "creator") or not member.get("can_post_messages"):
        log("ОШИБКА: бот не может публиковать в канал")
        problems += 1
    has_comments = bool(chat.get("linked_chat_id"))
    for n, post in enumerate(data["posts"], 1):
        cap = caption_for(post, has_comments)
        log("\n--- Пост %d · %s" % (n, post["title"]))
        log("подпись %d из 1024 (UTF-16), фото %d, кнопок %d" % (tg_len(cap), len(post["images"]), len(post.get("buttons") or [])))
        if tg_len(cap) > 1024:
            log("ОШИБКА: подпись длиннее 1024")
            problems += 1
        for img in post["images"]:
            status, size = head_ok(img["url"])
            log("  %s %s %d байт%s" % (status, img["url"].rsplit("/", 1)[-1], size, " · под спойлером" if img.get("spoiler") else ""))
            if status != 200 or size > 5 * 1024 * 1024:
                log("ОШИБКА: картинка недоступна или больше 5 МБ")
                problems += 1
        poll = post["poll"]
        kind = "викторина, верный «%s»" % poll["options"][poll["correct"]] if poll.get("quiz") else ("опрос, несколько ответов" if poll.get("multiple") else "опрос")
        log("  %s: %s (%d вариантов)" % (kind, poll["question"], len(poll["options"])))
        if tg_len(poll["question"]) > 300 or any(tg_len(o) > 100 for o in poll["options"]) or tg_len(poll.get("explanation") or "") > 200:
            log("ОШИБКА: вопрос, вариант или пояснение длиннее лимита")
            problems += 1
    log("\nИтог проверки: %s" % ("всё готово к публикации" if not problems else "проблем: %d" % problems))
    return 1 if problems else 0


def send_post(tg, post, caption, loud):
    base = {"chat_id": tg.chat_id}
    if not loud:
        base["disable_notification"] = True
    imgs = post["images"]
    if len(imgs) == 1:
        p = dict(base, photo=imgs[0]["url"], caption=caption, has_spoiler=bool(imgs[0].get("spoiler")))
        if post.get("buttons"):
            p["reply_markup"] = {"inline_keyboard": [[{"text": t, "url": u}] for t, u in post["buttons"]]}
        res = tg.call("sendPhoto", p)
        return [res["message_id"]], res
    media = []
    for i, img in enumerate(imgs):
        m = {"type": "photo", "media": img["url"], "has_spoiler": bool(img.get("spoiler"))}
        if i == 0:
            m["caption"] = caption
        media.append(m)
    res = tg.call("sendMediaGroup", dict(base, media=media))
    return [r["message_id"] for r in res], res[0]


def publish(tg, data, root):
    state_path = root / "kanal-start.json"
    state = load_state(state_path)
    _, chat, member = channel_info(tg)
    if not member.get("can_post_messages"):
        log("Бот не может публиковать в канал, остановка")
        return 1
    channel = chat.get("username") or ""
    has_comments = bool(chat.get("linked_chat_id"))
    log("Канал @%s, комментарии %s" % (channel, "включены" if has_comments else "выключены: пост 5 уйдёт с запасной концовкой"))
    state["channel"] = channel
    sent_news, failed = [], False
    first = True
    for n, post in enumerate(data["posts"], 1):
        rec = state["posts"].get(post["id"]) or {}
        if rec.get("poll_id"):
            log("Пост %d уже опубликован (сообщения %s), пропускаю" % (n, rec.get("message_ids")))
            first = False
            continue
        try:
            if not rec.get("message_ids"):
                if not first:
                    time.sleep(PAUSE)
                ids, res = send_post(tg, post, caption_for(post, has_comments), loud=(n == 1))
                rec = {"title": post["title"], "message_ids": ids, "date": now().strftime("%Y-%m-%d %H:%M")}
                state["posts"][post["id"]] = rec
                save_state(state_path, state)
                sent_news.append({"id": ids[0], "date": now().strftime("%Y-%m-%d"), "title": post["title"]})
                log("Пост %d отправлен: сообщения %s" % (n, ids))
            time.sleep(PAUSE)
            res = tg.call("sendPoll", poll_payload(tg.chat_id, post["poll"]))
            rec["poll_id"] = res["message_id"]
            save_state(state_path, state)
            log("Опрос к посту %d отправлен: сообщение %s" % (n, res["message_id"]))
        except TgError as err:
            log("Пост %d не отправлен: %s. Остановка, повторный запуск продолжит отсюда" % (n, err))
            failed = True
            break
        first = False
    if sent_news:
        record_news(root, channel, sent_news)
    p1 = (state["posts"].get(data["posts"][0]["id"]) or {}).get("message_ids")
    if not failed and p1 and not state.get("pinned"):
        try:
            tg.call("pinChatMessage", {"chat_id": tg.chat_id, "message_id": p1[0], "disable_notification": True})
            state["pinned"] = p1[0]
            log("Пост 1 закреплён (сообщение %s)" % p1[0])
        except TgError as err:
            state["pin_error"] = str(err)
            log("Закрепить пост 1 не вышло: %s. Закрепи вручную: зажать пост → «Закрепить»" % err)
        save_state(state_path, state)
    if channel:
        for post in data["posts"]:
            rec = state["posts"].get(post["id"]) or {}
            if rec.get("message_ids"):
                log("https://t.me/%s/%s · %s" % (channel, rec["message_ids"][0], post["title"]))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            for post in data["posts"]:
                rec = state["posts"].get(post["id"]) or {}
                f.write("- %s: сообщения %s, опрос %s\n" % (post["title"], rec.get("message_ids"), rec.get("poll_id")))
    return 1 if failed else 0


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Стартовые посты канала Каземата")
    ap.add_argument("mode", choices=["check", "publish"])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    a = ap.parse_args()
    root = Path(a.root)
    data = json.loads((Path(__file__).resolve().parent / "kanal_start.json").read_text(encoding="utf-8"))
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        log("секреты TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID не заданы")
        return 1
    tg = Telegram(token, chat)
    try:
        return check(tg, data) if a.mode == "check" else publish(tg, data, root)
    except TgError as err:
        log("Ошибка Telegram: %s" % err)
        return 1


if __name__ == "__main__":
    sys.exit(main())
