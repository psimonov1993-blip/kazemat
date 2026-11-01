#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Автопостинг Мастерской Каземата в Telegram-канал. Только стандартная библиотека Python 3.8+

Режимы:
  board    --before SHA --after SHA   посты о том, что изменилось в board.json между двумя коммитами
  board    --old FILE --new FILE      то же по двум файлам (для локальной проверки)
  release  --event FILE               пост о новой версии игры из события GitHub release
  release  --tag v1.1 [--name ...] [--body-file F]   то же вручную
  latest                              последний diff board.json и последний релиз, только печать
  hello                               приветственный пост канала (запускает автор вручную)
  selftest                            тихое тестовое сообщение и его немедленное удаление
  admintest                           проверочная копия последнего изменения доски только в личку организатору

Флаги: --dry-run печатает готовые посты и ничего не отправляет; --root корень сайта (по умолчанию два уровня выше скрипта)
Секреты берутся из окружения: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID. Если их нет, публикация пропускается, код выхода 0
Необязательный TELEGRAM_ADMIN_CHAT_ID: подробная копия каждого изменения доски и релиза в личку организатору
После отправки id постов дописываются в news.json, по нему сайт показывает ленту
"""
import argparse
import datetime
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SITE_URL = (os.environ.get("SITE_URL") or "https://psimonov1993-blip.github.io/kazemat/").rstrip("/") + "/"
REPO = os.environ.get("GITHUB_REPOSITORY") or "psimonov1993-blip/kazemat"
COVER_URL = "https://raw.githubusercontent.com/%s/main/img/07_vyhod.jpg" % REPO
MSK = datetime.timezone(datetime.timedelta(hours=3))
NEWS_KEEP = 30
PAUSE = 1.5  # пауза между постами, чтобы не упереться в лимит канала

STATUS_WORD = {"review": "на рассмотрении", "accepted": "принят", "done": "в игре", "returned": "на доработке"}
PLACE_TO = {
    "Лазарет": "к лазарету", "Казарма": "к казарме", "Кабинет коменданта": "к кабинету коменданта",
    "Галерея с газом": "к галерее с газом", "Пороховой погреб": "к пороховому погребу", "Потерна и выход": "к потерне",
}
PLACE_AT = {
    "Лазарет": "в лазарете", "Казарма": "в казарме", "Кабинет коменданта": "в кабинете коменданта",
    "Галерея с газом": "в галерее с газом", "Пороховой погреб": "в пороховом погребе", "Потерна и выход": "в потерне",
    "Вся игра": "по всему каземату",
}


def log(*parts):
    print(*parts, flush=True)


def e(text):
    return html.escape(str(text if text is not None else ""), quote=False)


def plural(n, forms):
    a = abs(int(n)) % 100
    b = a % 10
    if 10 < a < 20:
        return forms[2]
    if b == 1:
        return forms[0]
    if 2 <= b <= 4:
        return forms[1]
    return forms[2]


def orders_word(n):
    return plural(n, ("приказ", "приказа", "приказов"))


def seats_word(n):
    return plural(n, ("место", "места", "мест"))


def today():
    return datetime.datetime.now(MSK).strftime("%Y-%m-%d")


class Msg:
    def __init__(self, title, text, button=None, photo=None):
        self.title = title      # короткий заголовок для ленты на сайте
        self.text = text        # HTML для Telegram
        self.button = button    # (подпись, ссылка) или None
        self.photo = photo      # ссылка на обложку или None


# ---------- посты по изменениям board.json ----------

def type_id(order, board):
    if order.get("type_id"):
        return order["type_id"]
    for c in board.get("catalog") or []:
        if c.get("name") == order.get("type"):
            return c.get("id")
    return "other"


def accepted_phrase(t, place):
    to, at = PLACE_TO.get(place), PLACE_AT.get(place)
    return {
        "monster": "твой монстр уже ползёт " + (to or "по тёмным галереям"),
        "room": "твою комнату уже размечают на плане форта",
        "note": "краска для твоей надписи уже разведена",
        "prop": "твой предмет уже несут " + (to or "вниз, в каземат"),
        "rule": "новое правило уже вписывают в устав гарнизона",
        "sound": "твой звук уже настраивают" + (" " + at if at else ""),
        "egg": "тайник уже готовят. Никому не говори, где искать",
        "weapon": "оружие уже чистят и смазывают",
        "mech": "механику уже собирают в мастерской",
    }.get(t, "приказ ушёл в работу")


def done_phrase(t, place):
    at = PLACE_AT.get(place)
    room_at = at if at and at != "по всему каземату" else None
    return {
        "monster": "твой монстр уже ждёт " + (room_at or "в темноте"),
        "room": "твоя комната открыта для всех, кто до неё дойдёт",
        "note": "твоя надпись уже на стене" + (" " + room_at if room_at else ""),
        "prop": "твой предмет уже стоит " + (room_at or "на своём месте"),
        "rule": "новое правило действует",
        "sound": "твой звук уже слышно " + (at or "в каземате"),
        "egg": "тайник спрятан. Посмотрим, кто найдёт первым",
        "weapon": "оружие выдано гарнизону",
        "mech": "механика работает",
    }.get(t, "приказ выполнен")


def used_orders(board, callsign):
    return sum(int(o.get("cost") or 0) for o in board.get("orders") or []
               if o.get("callsign") == callsign and o.get("status") != "returned")


def seats_line(free, total):
    if free <= 0:
        return "Все %d %s заняты, набор в этом сезоне закрыт" % (total, seats_word(total))
    return "Свободных мест: %d из %d" % (free, total)


def order_body(o, board):
    lines = ["От: <b>%s</b>" % e(o.get("callsign"))]
    cost = int(o.get("cost") or 0)
    lines.append("Изменение: %s" % e(o.get("type") or "Другое") + (", %d %s" % (cost, orders_word(cost)) if cost else ""))
    if o.get("place"):
        lines.append("Где: %s" % e(o["place"]))
    out = "\n".join(lines)
    if o.get("secret"):
        out += "\n\n<i>Суть держим в секрете, пусть ищут</i>"
    elif o.get("text"):
        out += "\n<blockquote>%s</blockquote>" % e(o["text"])
    return out


def msg_new_order(o, board):
    n = o.get("n")
    status = o.get("status") or "review"
    if status == "accepted":
        tail = "Статус: принят, войдёт в следующую версию игры"
    elif status == "done":
        tail = "Статус: уже в игре" + (", версия %s" % e(o["version"]) if o.get("version") else "")
    elif status == "returned":
        tail = "Статус: на доработке"
    else:
        tail = "Статус: на рассмотрении, ответ обычно за 1-3 дня"
    text = "<b>Приказ № %s поступил в штаб</b>\n%s\n\n%s" % (e(n), order_body(o, board), tail)
    return Msg("Приказ № %s поступил в штаб" % n, text, ("Доска приказов", SITE_URL + "#doska"))


def msg_status(o, board):
    n, cs, st = o.get("n"), o.get("callsign") or "", o.get("status")
    t, place = type_id(o, board), o.get("place") or ""
    if st == "accepted":
        text = "<b>Приказ № %s принят</b>\n%s, %s\n\nВойдёт в следующую версию игры" % (e(n), e(cs), e(accepted_phrase(t, place)))
        return Msg("Приказ № %s принят" % n, text, ("Доска приказов", SITE_URL + "#doska"))
    if st == "done":
        ver = o.get("version")
        head = "Приказ № %s в игре с версии %s" % (n, ver) if ver else "Приказ № %s уже в игре" % n
        text = "<b>%s</b>\n%s, %s. Позывной вписан в летопись Каземата" % (e(head), e(cs), e(done_phrase(t, place)))
        return Msg(head, text, ("Скачать игру", SITE_URL + "#igra"))
    if st == "returned":
        text = ("<b>Приказ № %s вернулся на доработку</b>\nПозывной: %s\n\n"
                "В таком виде его не собрать, организатор объяснит, что поправить. "
                "Приказ не сгорает: его можно переписать или вернуть его часть взноса") % (e(n), e(cs))
        return Msg("Приказ № %s вернулся на доработку" % n, text, ("Доска приказов", SITE_URL + "#doska"))
    return None


def board_messages(old, new):
    msgs = []
    os_, ns = old.get("season") or {}, new.get("season") or {}
    total = int(ns.get("seats") or 12)
    garrison = [m for m in new.get("garrison") or [] if m.get("callsign")]
    free_now = max(total - len(garrison), 0)

    if os_.get("name") and ns.get("name") and os_["name"] != ns["name"]:
        text = "<b>Открыт %s</b>\nМест в гарнизоне: %d. Звания, цены и конструктор приказа на сайте" % (e(ns["name"]), total)
        msgs.append(Msg("Открыт %s" % ns["name"], text, ("Звания", SITE_URL + "#zvaniya")))
    elif os_.get("seats") is not None and int(os_.get("seats") or 0) != total:
        text = "<b>Мест в гарнизоне теперь %d</b>\nБыло %d. %s" % (total, int(os_["seats"]), seats_line(free_now, total))
        msgs.append(Msg("Мест в гарнизоне теперь %d" % total, text, ("Звания", SITE_URL + "#zvaniya")))

    old_ranks = {r.get("id"): r for r in old.get("ranks") or []}
    for r in new.get("ranks") or []:
        o = old_ranks.get(r.get("id"))
        price, cnt = r.get("price"), int(r.get("orders") or 0)
        if o is None:
            if old_ranks:
                text = "<b>Новое звание: %s</b>\n%s ₽ за сезон, %d %s" % (e(r.get("name")), e(price), cnt, orders_word(cnt))
                msgs.append(Msg("Новое звание: %s" % r.get("name"), text, ("Звания", SITE_URL + "#zvaniya")))
        elif o.get("price") != price or int(o.get("orders") or 0) != cnt:
            ocnt = int(o.get("orders") or 0)
            text = ("<b>Пропуск «%s»: новые условия</b>\nБыло: %s ₽, %d %s\nСтало: %s ₽, %d %s"
                    % (e(r.get("name")), e(o.get("price")), ocnt, orders_word(ocnt), e(price), cnt, orders_word(cnt)))
            msgs.append(Msg("Пропуск «%s»: новые условия" % r.get("name"), text, ("Звания", SITE_URL + "#zvaniya")))

    old_members = {str(m.get("callsign")).casefold(): m for m in old.get("garrison") or [] if m.get("callsign")}
    for idx, m in enumerate(garrison):
        om = old_members.get(str(m["callsign"]).casefold())
        cnt = int(m.get("orders") or 0)
        if om is None:
            free = max(total - (idx + 1), 0)
            text = ("<b>В гарнизоне пополнение</b>\nПозывной <b>%s</b>, звание %s. В запасе %d %s\n\n%s"
                    % (e(m["callsign"]), e(m.get("rank")), cnt, orders_word(cnt), seats_line(free, total)))
            msgs.append(Msg("В строю: %s" % m["callsign"], text, ("Гарнизон", SITE_URL + "#garnizon")))
        elif om.get("rank") != m.get("rank"):
            left = max(cnt - used_orders(new, m["callsign"]), 0)
            text = "<b>%s теперь %s</b>\nПриказов в запасе: %d из %d" % (e(m["callsign"]), e(m.get("rank")), left, cnt)
            msgs.append(Msg("%s теперь %s" % (m["callsign"], m.get("rank")), text, ("Гарнизон", SITE_URL + "#garnizon")))

    old_orders = {o.get("n"): o for o in old.get("orders") or []}
    for o in sorted((x for x in new.get("orders") or [] if x.get("n") is not None), key=lambda x: int(x["n"])):
        oo = old_orders.get(o["n"])
        if oo is None:
            msgs.append(msg_new_order(o, new))
        elif oo.get("status") != o.get("status") or (o.get("status") == "done" and oo.get("version") != o.get("version")):
            m = msg_status(o, new)
            if m:
                msgs.append(m)
    return msgs


# ---------- подробная копия организатору в личку ----------

def admin_report(old, new, title=""):
    """Все изменения доски одним сообщением, с полными текстами приказов. None, если доска не менялась"""
    lines = []
    total = int((new.get("season") or {}).get("seats") or 12)
    garrison = [m for m in new.get("garrison") or [] if m.get("callsign")]
    old_m = {str(m.get("callsign")).casefold(): m for m in old.get("garrison") or [] if m.get("callsign")}
    new_m = {str(m.get("callsign")).casefold(): m for m in garrison}
    for k, m in new_m.items():
        om = old_m.get(k)
        if om is None:
            lines.append("Новый боец: <b>%s</b>, %s, приказов %s. Свободно мест %d из %d"
                         % (e(m["callsign"]), e(m.get("rank")), e(m.get("orders")), max(total - len(garrison), 0), total))
        elif om.get("rank") != m.get("rank"):
            lines.append("Звание: <b>%s</b> %s → %s" % (e(m["callsign"]), e(om.get("rank")), e(m.get("rank"))))
    for k, m in old_m.items():
        if k not in new_m:
            lines.append("Убран из гарнизона: <b>%s</b> (%s)" % (e(m.get("callsign")), e(m.get("rank"))))

    old_o = {o.get("n"): o for o in old.get("orders") or []}
    new_o = {o.get("n"): o for o in new.get("orders") or [] if o.get("n") is not None}
    for n in sorted(new_o, key=int):
        o, oo = new_o[n], old_o.get(n)
        head = "№ %s от <b>%s</b>: %s, %s %s, %s" % (e(n), e(o.get("callsign")), e(o.get("type")), e(o.get("cost")),
                                                   orders_word(int(o.get("cost") or 0)), e(o.get("place")))
        st = STATUS_WORD.get(o.get("status"), o.get("status")) + (" v%s" % o["version"] if o.get("version") else "")
        if oo is None:
            lines.append("Новый приказ %s. Статус: %s%s\n<blockquote>%s</blockquote>"
                         % (head, e(st), ", секретный" if o.get("secret") else "", e(o.get("text"))))
        else:
            if oo.get("status") != o.get("status") or oo.get("version") != o.get("version"):
                ost = STATUS_WORD.get(oo.get("status"), oo.get("status")) + (" v%s" % oo["version"] if oo.get("version") else "")
                lines.append("Приказ %s: %s → <b>%s</b>" % (head, e(ost), e(st)))
            if oo.get("text") != o.get("text"):
                lines.append("Приказ № %s: текст изменён\n<blockquote>%s</blockquote>" % (e(n), e(o.get("text"))))
    for n, o in old_o.items():
        if n not in new_o:
            lines.append("Приказ № %s от %s удалён с доски" % (e(n), e(o.get("callsign"))))

    os_, ns = old.get("season") or {}, new.get("season") or {}
    if os_ != ns:
        lines.append("Сезон: %s, мест %s → %s, мест %s" % (e(os_.get("name")), e(os_.get("seats")), e(ns.get("name")), e(ns.get("seats"))))
    old_r = {r.get("id"): r for r in old.get("ranks") or []}
    for r in new.get("ranks") or []:
        o = old_r.get(r.get("id"))
        if o and (o.get("price"), o.get("orders")) != (r.get("price"), r.get("orders")):
            lines.append("Звание %s: %s ₽ / %s → %s ₽ / %s" % (e(r.get("name")), e(o.get("price")), e(o.get("orders")), e(r.get("price")), e(r.get("orders"))))
    covered = {"season", "ranks", "garrison", "orders"}
    other = sorted(k for k in set(old) | set(new) if k not in covered and old.get(k) != new.get(k))
    if other:
        lines.append("Изменены разделы: %s" % ", ".join(e(k) for k in other))
    if not lines:
        return None
    text = "<b>Доска Каземата изменилась</b>" + ("\n%s" % e(title) if title else "") + "\n\n" + "\n\n".join(lines)
    if len(text) > 3900:
        text = text[:3900].rsplit("\n", 1)[0] + "\n…"
    return Msg("Копия организатору", text)


def admin_release(msg, url=""):
    text = "<b>Копия: пост о релизе</b>" + ("\n%s" % e(url) if url else "") + "\n\n" + msg.text
    return Msg("Копия организатору", text[:3900])


def commit_title(root, sha):
    if not sha:
        return ""
    out = subprocess.run(["git", "log", "-1", "--format=%h %s", sha], cwd=root, capture_output=True)
    return out.stdout.decode("utf-8", "replace").strip()


# ---------- пост о релизе ----------

def release_body(body):
    out = []
    for raw in (body or "").replace("\r", "").split("\n"):
        line = raw.strip()
        if not line or "Full Changelog" in line or line.startswith("<!--"):
            if out and out[-1] != "":
                out.append("")
            continue
        line = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", line)
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = line.replace("`", "")
        heading = line.startswith("#")
        line = line.lstrip("#").strip()
        bullet = re.match(r"^[-*+]\s+(.*)$", line)
        if bullet:
            line = "· " + bullet.group(1)
        line = e(line)
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        out.append("<b>%s</b>" % line if heading else line)
    while out and out[-1] == "":
        out.pop()
    text = "\n".join(out)
    if len(text) > 2500:
        text = text[:2500].rsplit("\n", 1)[0] + "\n…"
    return text


def release_message(tag, name, body, board):
    ver = re.sub(r"^[vV]", "", tag or "").strip() or (name or "")
    parts = ["<b>КАЗЕМАТ %s: новая версия на сайте</b>" % e(ver)]
    if name and ver not in name:
        parts[0] += "\n" + e(name)
    changes = release_body(body)
    if changes:
        parts.append(changes if changes.startswith("<b>") else "Что изменилось:\n" + changes)
    authors = [o for o in board.get("orders") or []
               if o.get("status") == "done" and re.sub(r"^[vV]", "", str(o.get("version") or "")) == ver]
    if authors:
        parts.append("Приказы гарнизона в этой версии: " + ", ".join("№ %s %s" % (e(o.get("n")), e(o.get("callsign"))) for o in authors))
    parts.append('Скачать для Android: <a href="%s#igra">сайт Мастерской</a>' % SITE_URL)
    return Msg("КАЗЕМАТ %s" % ver, "\n\n".join(parts), ("Скачать игру", SITE_URL + "#igra"), COVER_URL)


def hello_message():
    text = ("<b>Штаб Каземата на связи</b>\n"
            "КАЗЕМАТ - хоррор про русский форт 1915 года, который делают сами игроки\n\n"
            "Сюда сразу приходят:\n"
            "· новые бойцы гарнизона и свободные места\n"
            "· каждый приказ и его судьба: принят, в игре, на доработке\n"
            "· каждая новая версия игры\n\n"
            "Скачать игру и составить приказ можно на сайте")
    return Msg("Штаб Каземата на связи", text, ("Открыть сайт", SITE_URL), COVER_URL)


# ---------- Telegram ----------

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
                with urllib.request.urlopen(req, timeout=40) as r:
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

    def deliver(self, msg, silent=False):
        base = {"chat_id": self.chat_id, "parse_mode": "HTML"}
        if silent:
            base["disable_notification"] = True
        if msg.button:
            base["reply_markup"] = {"inline_keyboard": [[{"text": msg.button[0], "url": msg.button[1]}]]}
        if msg.photo and visible_len(msg.text) <= 1024:
            try:
                return self.call("sendPhoto", dict(base, photo=msg.photo, caption=msg.text))
            except TgError as err:
                log("Обложка не ушла (%s), отправляю текстом" % err)
        payload = dict(base, text=msg.text)
        if msg.photo:
            payload["link_preview_options"] = {"url": msg.photo, "prefer_large_media": True, "show_above_text": True}
        else:
            payload["link_preview_options"] = {"is_disabled": True}
        return self.call("sendMessage", payload)


def visible_len(text):
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


# ---------- news.json, вывод, запуск ----------

def plain(text):
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def show(msgs, header):
    log(header)
    for i, m in enumerate(msgs, 1):
        btn = " · кнопка «%s» -> %s" % m.button if m.button else ""
        cover = " · с обложкой" if m.photo else ""
        log("\n--- Пост %d из %d · %s%s%s" % (i, len(msgs), m.title, btn, cover))
        log(m.text)
    log("")


def summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def record_news(root, channel, sent):
    path = root / "news.json"
    try:
        news = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        news = {"channel": "", "posts": []}
    posts = news.get("posts") or []
    if channel and news.get("channel") and news["channel"] != channel:
        posts = []
    posts.extend(sent)
    news = {"channel": channel or news.get("channel") or "", "posts": posts[-NEWS_KEEP:]}
    path.write_text(json.dumps(news, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log("news.json: дописано постов %d" % len(sent))


def send_admin(admin, token, dry_run):
    """Копия в личку организатору. Без TELEGRAM_ADMIN_CHAT_ID молча пропускается, ошибка не роняет запуск"""
    if admin is None:
        return
    chat = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()
    if dry_run:
        show([admin], "Копия организатору в личку (%s):" % ("будет отправлена" if chat else "секрет TELEGRAM_ADMIN_CHAT_ID не задан, не отправится"))
        return
    if not chat or not token:
        return
    try:
        res = Telegram(token, chat).deliver(admin)
        log("Копия организатору отправлена, message_id %s" % res.get("message_id"))
    except TgError as err:
        log("Копия организатору не ушла: %s (публикацию в канале это не отменяет)" % err)


def publish(msgs, root, dry_run, admin=None):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not msgs:
        log("Изменений для канала нет, публиковать нечего")
        summary(["Изменений для канала нет"])
        send_admin(admin, token, dry_run)
        return 0
    if dry_run:
        show(msgs, "Режим проверки: посты собраны, в канал не отправлены")
        summary(["Проверка: %d пост(ов) собрано, не отправлено" % len(msgs)] + ["- " + m.title for m in msgs])
        send_admin(admin, token, dry_run)
        return 0
    if not token or not chat:
        show(msgs, "секреты не заданы, публикация пропущена")
        summary(["Секреты не заданы, публикация пропущена"] + ["- " + m.title for m in msgs])
        return 0
    send_admin(admin, token, dry_run)
    tg = Telegram(token, chat)
    sent, failed, channel = [], 0, ""
    for i, m in enumerate(msgs):
        if i:
            time.sleep(PAUSE)
        try:
            res = tg.deliver(m)
        except TgError as err:
            failed += 1
            log("Не отправлено «%s»: %s" % (m.title, err))
            continue
        channel = (res.get("chat") or {}).get("username") or channel
        sent.append({"id": res["message_id"], "date": today(), "title": m.title})
        log("Отправлено: «%s», message_id %s" % (m.title, res["message_id"]))
    if not channel and chat.startswith("@"):
        channel = chat[1:]
    if sent:
        record_news(root, channel, sent)
    summary(["Отправлено %d из %d" % (len(sent), len(msgs))] + ["- %s (id %s)" % (s["title"], s["id"]) for s in sent])
    return 1 if failed else 0


def git_board(root, sha):
    if not sha or set(sha) == {"0"}:
        return None
    try:
        out = subprocess.run(["git", "show", "%s:board.json" % sha], cwd=root, capture_output=True, check=True)
    except subprocess.CalledProcessError:
        return None
    return json.loads(out.stdout.decode("utf-8-sig"))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def latest_release():
    req = urllib.request.Request("https://api.github.com/repos/%s/releases/latest" % REPO,
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "kazemat-tg"})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        log("Последний релиз не прочитан: HTTP %s" % err.code)
        return None


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description="Автопостинг Каземата в Telegram")
    ap.add_argument("mode", choices=["board", "release", "latest", "hello", "selftest", "admintest"])
    ap.add_argument("--before")
    ap.add_argument("--after")
    ap.add_argument("--old")
    ap.add_argument("--new")
    ap.add_argument("--event")
    ap.add_argument("--tag")
    ap.add_argument("--name", default="")
    ap.add_argument("--body-file")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    root = Path(a.root)

    if a.mode == "board":
        title = ""
        if a.old or a.new:
            old, new = read_json(a.old), read_json(a.new)
        else:
            title = commit_title(root, a.after or "HEAD")
            new = git_board(root, a.after or "HEAD")
            old = git_board(root, a.before)
            if new is None:
                log("board.json в новом коммите не найден, публиковать нечего")
                return 0
            if old is None:
                log("Прошлой версии board.json нет (новая ветка или перезапись истории), сравнивать не с чем")
                return 0
        return publish(board_messages(old, new), root, a.dry_run, admin_report(old, new, title))

    if a.mode == "release":
        board = read_json(root / "board.json") if (root / "board.json").exists() else {}
        if a.event:
            rel = (read_json(a.event) or {}).get("release") or {}
            if rel.get("draft"):
                log("Черновик релиза, публиковать рано")
                return 0
            tag, name, body, url = rel.get("tag_name"), rel.get("name") or "", rel.get("body") or "", rel.get("html_url") or ""
        else:
            tag, name, url = a.tag, a.name, ""
            body = Path(a.body_file).read_text(encoding="utf-8") if a.body_file else ""
        msg = release_message(tag, name, body, board)
        return publish([msg], root, a.dry_run, admin_release(msg, url))

    if a.mode == "latest":
        out = subprocess.run(["git", "log", "-n", "1", "--format=%H", "--", "board.json"], cwd=root, capture_output=True, text=True)
        sha = out.stdout.strip()
        msgs = []
        if sha:
            new, old = git_board(root, sha), git_board(root, sha + "^")
            log("Последнее изменение board.json: %s" % sha[:7])
            if new is not None and old is not None:
                msgs += board_messages(old, new)
        rel = latest_release()
        if rel:
            board = read_json(root / "board.json")
            msgs.append(release_message(rel.get("tag_name"), rel.get("name") or "", rel.get("body") or "", board))
        return publish(msgs, root, True)

    if a.mode == "hello":
        return publish([hello_message()], root, a.dry_run)

    if a.mode == "admintest":
        token, admin_chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()
        out = subprocess.run(["git", "log", "-n", "1", "--format=%H", "--", "board.json"], cwd=root, capture_output=True, text=True)
        sha = out.stdout.strip()
        report = None
        if sha:
            new, old = git_board(root, sha), git_board(root, sha + "^")
            if new is not None and old is not None:
                report = admin_report(old, new, commit_title(root, sha))
        if report is None:
            report = Msg("Копия организатору", "Изменений доски пока не было")
        report.text = "<b>Проверка: так выглядит копия изменений</b>\nВ канал это не публикуется\n\n" + report.text
        if a.dry_run or not token or not admin_chat:
            show([report], "Проверка копии: %s" % ("dry-run" if a.dry_run else "секрет TELEGRAM_ADMIN_CHAT_ID или токен не задан, не отправлено"))
            return 0
        try:
            res = Telegram(token, admin_chat).deliver(report)
        except TgError as err:
            log("Копия организатору не ушла: %s" % err)
            return 1
        log("Проверочная копия отправлена организатору в личку, message_id %s" % res.get("message_id"))
        summary(["Проверочная копия отправлена в личку, message_id %s" % res.get("message_id")])
        return 0

    if a.mode == "selftest":
        token, chat = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(), os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat:
            log("секреты не заданы, публикация пропущена")
            return 0
        if a.dry_run:
            log("Проверка связи в режиме dry-run: ничего не отправлено")
            return 0
        tg = Telegram(token, chat)
        try:
            res = tg.deliver(Msg("Проверка связи", "Проверка связи штаба. Сообщение сейчас удалится"), silent=True)
        except TgError as err:
            log("Тестовое сообщение не отправлено: %s" % err)
            return 1
        mid = res["message_id"]
        log("Тихое тестовое сообщение отправлено: message_id %s, канал @%s" % (mid, (res.get("chat") or {}).get("username") or "?"))
        time.sleep(1)
        try:
            ok = tg.call("deleteMessage", {"chat_id": chat, "message_id": mid})
        except TgError as err:
            log("Удалить сообщение %s не удалось: %s. Удали его в канале вручную" % (mid, err))
            return 1
        log("Удаление сообщения %s: %s" % (mid, "успешно" if ok is True else ok))
        summary(["Проверка связи: message_id %s отправлено тихо и удалено (%s)" % (mid, ok)])
        return 0 if ok is True else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
