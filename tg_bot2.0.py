#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tg_bot.py — автономный Telegram-бот: ищет малый бизнес вокруг адреса,
проверяет сайты и показывает результат прямо в чате.

Команды:
  /start   — выбор языка (🇷🇺 Русский / 🇬🇧 English) кнопками;
  /search  — поиск: /search [адрес] [радиус в метрах]
             пример: /search Реутов, улица Октября 44 1000
  /report  — статистика + меню с кнопками «Без сайта» и «Сломанные сайты»;
  /nosite  — сразу открыть список 🔥 «Без сайта»;
  /broken  — сразу открыть список 🛠 «Сломанные сайты».

Как устроен /search:
  * каждый поиск работает в отдельном потоке (threading), поэтому бот
    остаётся доступным для других команд и других пользователей;
  * результаты каждого пользователя лежат в своей папке results/<id>/,
    поэтому одновременные поиски не мешают друг другу;
  * сайты проверяются параллельно (пул потоков) — так поиск занимает минуты, а не часы.

Нужен файл find_businesses.py в той же папке — из него берутся функции поиска.

Установка:   pip install pyTelegramBotAPI requests
Запуск:      python tg_bot.py

Токен бота выдаёт @BotFather в Telegram (команда /newbot).
"""

# ---------------------------------------------------------------------------
# ВСТАВЬТЕ СЮДА СВОЙ ТОКЕН
# ---------------------------------------------------------------------------
TOKEN = "9033195296:AAGROtZv2M-H_H8fOsL-2vbvV_QXTfC49rk"

import csv
import html
import math
import os
import re
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

import telebot
from telebot import types

# Функции поиска берём из нашего первого скрипта (в нём есть защита
# `if __name__ == "__main__"`, поэтому при импорте он сам не запускается)
from find_businesses import (
    GROUP_ERROR,
    GROUP_NO_SITE,
    GROUP_OK,
    OUTPUT_FILES,
    build_query,
    check_website,
    geocode,
    overpass_request,
    parse_elements,
    save_csv,
)

# Если токен не заменили — сразу говорим об этом, а не падаем с непонятной ошибкой
if TOKEN == "ВСТАВЬ_СЮДА_ТОКЕН":
    sys.exit("Впишите токен бота в переменную TOKEN в начале файла tg_bot.py")

# Создаём объект бота: через него мы принимаем и отправляем сообщения.
# Telegram-библиотека сама обрабатывает входящие сообщения в нескольких потоках.
bot = telebot.TeleBot(TOKEN)

# ---------------------------------------------------------------------------
# ДАННЫЕ И НАСТРОЙКИ
# ---------------------------------------------------------------------------

# Здесь запоминаем язык каждого пользователя: {id_пользователя: "ru" или "en"}.
# Словарь живёт в памяти, поэтому после перезапуска бота выбор нужно сделать заново.
user_langs = {}

# Папка, где лежит этот скрипт, и папка с результатами поиска.
# У каждого пользователя своя подпапка: results/<id_пользователя>/
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "results")

# Все файлы, которые участвуют в статистике
REPORT_FILES = {
    "no_site": "no_site.csv",
    "error_site": "error_site.csv",
    "has_site": "has_site.csv",
}

# Два списка, которые можно листать: из какого файла берём данные,
# какой у них заголовок и показывать ли ссылку на сайт со статусом.
LISTS = {
    "no_site": {"file": "no_site.csv", "title_key": "list_no_site", "show_site": False},
    "error_site": {"file": "error_site.csv", "title_key": "list_error", "show_site": True},
}

PAGE_SIZE = 10   # сколько заведений на одной странице
MAX_FIELD = 80   # длинные названия/адреса/ссылки обрезаем до этой длины,
                 # чтобы страница гарантированно влезала в лимит Telegram (4096 символов)

# --- Ограничения для /search (бот публичный, а OSM-сервисы бесплатные) ---
MIN_RADIUS = 100             # минимальный радиус, метров
MAX_RADIUS = 5000            # максимальный радиус, метров
MAX_BUSINESSES = 1000        # сколько заведений максимум проверяем за один поиск
MAX_PARALLEL_SEARCHES = 3    # сколько поисков могут идти одновременно (у всех пользователей)
CHECK_WORKERS = 20           # сколько сайтов проверяем одновременно внутри одного поиска

# --- Защита от «перегруза» чужих бесплатных сервисов ---
# Nominatim разрешает не больше 1 запроса в секунду. geocode() после запроса
# спит 1 секунду, поэтому достаточно пускать в него по одному потоку за раз.
GEOCODE_LOCK = threading.Lock()
# Публичный Overpass тоже не любит много запросов сразу — пускаем не больше двух.
OVERPASS_SLOTS = threading.Semaphore(2)

# Кто сейчас ищет: {id_пользователя}. Нужен, чтобы один человек не запустил
# десять поисков подряд. Доступ к множеству защищён замком (Lock),
# потому что с ним работают разные потоки.
active_users = set()
active_lock = threading.Lock()

# Значения, которые считаем «адреса нет» (в старых CSV из прошлых версий
# find_businesses.py в пустом адресе стояло «Нет адреса»)
EMPTY_ADDRESSES = {"", "нет адреса", "no address"}

# Шаблон для разбора «/search Адрес Радиус»: всё до последнего числа — адрес,
# последнее число — радиус (после него допускается буква «м» или «m»)
SEARCH_ARGS = re.compile(r"(.+)\s+(\d+)\s*(?:м|m)?", re.IGNORECASE | re.DOTALL)

# Подсказка при выборе языка — на двух языках сразу, ведь язык ещё не выбран
CHOOSE_LANG_TEXT = "Выберите язык / Choose your language:"

# Все тексты бота на двух языках. Чтобы добавить язык, скопируйте один из
# блоков, переведите значения и добавьте кнопку в функции language_keyboard().
TEXTS = {
    "ru": {
        "lang_set": (
            "✅ Язык установлен: Русский\n\n"
            "Команды:\n"
            "/search [адрес] [радиус] — найти бизнесы рядом\n"
            "/report — статистика и меню списков\n"
            "/nosite — 🔥 клиенты без сайта\n"
            "/broken — 🛠 клиенты со сломанным сайтом"
        ),
        "report_title": "📊 Отчёт по найденным бизнесам",
        "total": "Всего найдено",
        "no_site": "🔥 Без сайта (горячие)",
        "error_site": "🛠 Сайт сломан (тёплые)",
        "has_site": "✅ Сайт работает (холодные)",
        "missing": "⚠️ Не найдены файлы: {files}",
        "no_files": (
            "Результатов пока нет.\n"
            "Запустите поиск: /search [адрес] [радиус в метрах]\n"
            "Пример: /search Реутов, улица Октября 44 1000"
        ),
        "choose_list": "Выберите список:",
        "btn_no_site": "🔥 Без сайта ({count})",
        "btn_error": "🛠 Сломанные сайты ({count})",
        "btn_back": "⬅️ Назад",
        "btn_next": "Вперёд ➡️",
        "btn_menu": "📊 Меню",
        "list_no_site": "🔥 Без сайта",
        "list_error": "🛠 Сломанные сайты",
        "list_ok": "✅ Сайт работает",
        "page_info": "Страница {page} из {pages} · всего {total}",
        "list_empty": "Список пуст. Сначала выполните поиск: /search [адрес] [радиус в метрах]",
        "no_name": "Без названия",
        "address_unknown": "Адрес не указан",
        # --- /search ---
        "search_started": (
            "⏳ Принял! Начинаю поиск. Если мест много, проверка сайтов может занять "
            "до 5 минут. Я пришлю отчет, как только закончу."
        ),
        "search_usage": (
            "Использование: /search [адрес] [радиус в метрах]\n"
            "Пример: /search Реутов, улица Октября 44 1000"
        ),
        "search_bad_radius": (
            "Радиус должен быть от {min} до {max} метров.\n"
            "Пример: /search Реутов, улица Октября 44 1000"
        ),
        "search_busy_user": "⏳ Ваш предыдущий поиск ещё не закончен. Дождитесь отчёта.",
        "search_busy_server": "🚧 Сейчас идёт слишком много поисков. Попробуйте через несколько минут.",
        "search_empty": "🤷 Ничего не найдено по адресу «{address}». Попробуйте увеличить радиус.",
        "search_truncated": (
            "⚠️ Найдено объектов: {found}. Проверю первые {checked} — "
            "чтобы охватить все, уменьшите радиус."
        ),
        "search_done": "✅ Готово! Результаты поиска: {address}, радиус {radius} м.",
        "search_failed": "❌ Поиск не удался: {reason}",
        "search_error": "❌ Произошла непредвиденная ошибка. Попробуйте позже.",
    },
    "en": {
        "lang_set": (
            "✅ Language set: English\n\n"
            "Commands:\n"
            "/search [address] [radius] — find businesses nearby\n"
            "/report — statistics and list menu\n"
            "/nosite — 🔥 businesses without a website\n"
            "/broken — 🛠 businesses with a broken website"
        ),
        "report_title": "📊 Business report",
        "total": "Total found",
        "no_site": "🔥 No website (hot leads)",
        "error_site": "🛠 Broken website (warm leads)",
        "has_site": "✅ Website works (cold leads)",
        "missing": "⚠️ Files not found: {files}",
        "no_files": (
            "No results yet.\n"
            "Start a search: /search [address] [radius in meters]\n"
            "Example: /search Reutov, Oktyabrya street 44 1000"
        ),
        "choose_list": "Choose a list:",
        "btn_no_site": "🔥 No website ({count})",
        "btn_error": "🛠 Broken websites ({count})",
        "btn_back": "⬅️ Back",
        "btn_next": "Next ➡️",
        "btn_menu": "📊 Menu",
        "list_no_site": "🔥 No website",
        "list_error": "🛠 Broken websites",
        "list_ok": "✅ Website works",
        "page_info": "Page {page} of {pages} · total {total}",
        "list_empty": "The list is empty. Run a search first: /search [address] [radius in meters]",
        "no_name": "Unnamed",
        "address_unknown": "Address unknown",
        # --- /search ---
        "search_started": (
            "⏳ Got it! Starting the search. If there are many places, checking the "
            "websites may take up to 5 minutes. I'll send the report as soon as I'm done."
        ),
        "search_usage": (
            "Usage: /search [address] [radius in meters]\n"
            "Example: /search Reutov, Oktyabrya street 44 1000"
        ),
        "search_bad_radius": (
            "The radius must be between {min} and {max} meters.\n"
            "Example: /search Reutov, Oktyabrya street 44 1000"
        ),
        "search_busy_user": "⏳ Your previous search is still running. Please wait for the report.",
        "search_busy_server": "🚧 Too many searches are running right now. Please try again in a few minutes.",
        "search_empty": "🤷 Nothing found near “{address}”. Try a larger radius.",
        "search_truncated": (
            "⚠️ Objects found: {found}. I'll check the first {checked} — "
            "use a smaller radius to cover them all."
        ),
        "search_done": "✅ Done! Search results: {address}, radius {radius} m.",
        "search_failed": "❌ Search failed: {reason}",
        "search_error": "❌ An unexpected error occurred. Please try again later.",
    },
}


# ---------------------------------------------------------------------------
# ЧТЕНИЕ ДАННЫХ И ПОДГОТОВКА ТЕКСТА
# ---------------------------------------------------------------------------

def user_dir(user_id):
    """Папка с результатами конкретного пользователя: results/<id>/."""
    return os.path.join(DATA_DIR, str(user_id))


def load_rows(filename, user_id):
    """
    Читает CSV-файл пользователя и возвращает список строк (каждая строка —
    словарь вида {"Название": ..., "Адрес": ..., "Сайт": ..., "Широта": ..., ...}).
    Если файла нет или его не удалось прочитать — возвращает None.
    Файл читается при каждом обращении, поэтому бот всегда видит свежие данные.
    """
    path = os.path.join(user_dir(user_id), filename)
    if not os.path.exists(path):
        return None
    try:
        # utf-8-sig — именно в этой кодировке find_businesses.py сохраняет файлы
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    except (OSError, UnicodeDecodeError):
        return None


def raw(value):
    """Значение из CSV без пробелов по краям (None превращается в пустую строку)."""
    return (value or "").strip()


def clean(value):
    """То же, что raw(), но слишком длинный текст обрезается многоточием."""
    text = raw(value)
    if len(text) > MAX_FIELD:
        text = text[: MAX_FIELD - 1] + "…"
    return text


def esc(text):
    """
    Экранирует символы <, > и &, чтобы название вроде «Tom & Jerry <bar>»
    не сломало HTML-разметку сообщения.
    """
    return html.escape(text, quote=False)


def parse_coord(value):
    """Превращает значение из CSV в число. Если не получилось — возвращает None."""
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def format_location(row, t):
    """
    Строка с местоположением заведения:
      * есть адрес                -> «📍 Ленина 5»
      * адреса нет, есть координаты -> «📍 55.75123, 37.61234» (кликабельная ссылка
                                      на Яндекс.Карты с меткой на этом месте)
      * нет ни того, ни другого   -> «📍 Адрес не указан»
    """
    address = clean(row.get("Адрес"))
    if address.lower() not in EMPTY_ADDRESSES:
        return f"📍 {esc(address)}"

    lat = parse_coord(row.get("Широта"))
    lon = parse_coord(row.get("Долгота"))
    if lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
        # В Яндекс.Картах порядок «долгота,широта» (а не наоборот!)
        url = f"https://yandex.ru/maps/?ll={lon},{lat}&pt={lon},{lat}&z=17&l=map"
        return f'📍 <a href="{url}">{lat:.5f}, {lon:.5f}</a>'

    return f"📍 {t['address_unknown']}"


def format_entry(number, row, show_site, t):
    """
    Одна запись списка в HTML-разметке Telegram:
        1. <жирное название>
         📍 адрес (или координаты)
         🌐 ссылка на сайт      <- только для сломанных сайтов
         ⚠️ статус ошибки       <- только для сломанных сайтов
    """
    name = esc(clean(row.get("Название"))) or t["no_name"]
    lines = [f"{number}. <b>{name}</b>", f" {format_location(row, t)}"]

    if show_site:
        site = raw(row.get("Сайт"))
        if site:
            # В OSM сайт иногда записан без http:// — добавляем, иначе ссылка не откроется
            href = site if site.lower().startswith(("http://", "https://")) else "http://" + site
            href = html.escape(href, quote=True)
            lines.append(f' 🌐 <a href="{href}">{esc(clean(site))}</a>')

        status = clean(row.get("Статус"))
        if status:
            lines.append(f" ⚠️ {esc(status)}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# КЛАВИАТУРЫ (КНОПКИ ПОД СООБЩЕНИЯМИ)
# ---------------------------------------------------------------------------

def language_keyboard():
    """Кнопки выбора языка."""
    keyboard = types.InlineKeyboardMarkup()
    # callback_data — «секретный» текст кнопки: бот получит его при нажатии
    keyboard.add(
        types.InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
        types.InlineKeyboardButton("🇬🇧 English", callback_data="lang_en"),
    )
    return keyboard


def menu_button(t):
    """Кнопка «📊 Меню» — возвращает к статистике и выбору списка."""
    return types.InlineKeyboardButton(t["btn_menu"], callback_data="menu")


def pagination_keyboard(group, page, pages, t):
    """
    Кнопки под списком:
      [⬅️ Назад] [2/25] [Вперёд ➡️]
      [📊 Меню]
    «Назад» на первой странице и «Вперёд» на последней не показываются.
    Данные страницы зашиты прямо в callback_data («page:группа:номер»),
    поэтому боту не нужно ничего запоминать между нажатиями.
    """
    keyboard = types.InlineKeyboardMarkup()

    row = []
    if page > 0:
        row.append(types.InlineKeyboardButton(t["btn_back"], callback_data=f"page:{group}:{page - 1}"))
    # Кнопка-счётчик страниц: при нажатии ничего не делает ("noop")
    row.append(types.InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        row.append(types.InlineKeyboardButton(t["btn_next"], callback_data=f"page:{group}:{page + 1}"))

    keyboard.row(*row)
    keyboard.row(menu_button(t))
    return keyboard


# ---------------------------------------------------------------------------
# СБОРКА СООБЩЕНИЙ: МЕНЮ И СТРАНИЦЫ СПИСКОВ
# ---------------------------------------------------------------------------

def build_menu(lang, user_id):
    """
    Собирает главное сообщение /report: статистика + кнопки выбора списка.
    Данные берутся из результатов ЭТОГО пользователя.
    Возвращает пару (текст, клавиатура). Если результатов нет — клавиатуры нет (None).
    """
    t = TEXTS[lang]

    # Читаем все файлы: {"no_site": [строки] или None, ...}
    data = {key: load_rows(name, user_id) for key, name in REPORT_FILES.items()}

    # Оставляем только те файлы, которые удалось прочитать
    found = {key: rows for key, rows in data.items() if rows is not None}
    if not found:
        return t["no_files"], None

    total = sum(len(rows) for rows in found.values())
    lines = [f"<b>{t['report_title']}</b>", "", f"{t['total']}: {total}"]
    for key in REPORT_FILES:
        rows = data[key]
        # Если файла нет — вместо числа ставим прочерк
        lines.append(f"{t[key]}: {len(rows) if rows is not None else '—'}")

    # Если каких-то файлов не хватает — предупреждаем об этом
    missing = [REPORT_FILES[key] for key, rows in data.items() if rows is None]
    if missing:
        lines += ["", t["missing"].format(files=", ".join(missing))]

    lines += ["", t["choose_list"]]

    # Две кнопки (каждая в своей строке) с количеством записей в названии
    def count_label(rows):
        return len(rows) if rows is not None else "—"

    keyboard = types.InlineKeyboardMarkup()
    keyboard.row(types.InlineKeyboardButton(
        t["btn_no_site"].format(count=count_label(data["no_site"])),
        callback_data="page:no_site:0",
    ))
    keyboard.row(types.InlineKeyboardButton(
        t["btn_error"].format(count=count_label(data["error_site"])),
        callback_data="page:error_site:0",
    ))

    return "\n".join(lines), keyboard


def build_page(lang, group, page, user_id):
    """
    Собирает одну страницу списка (по PAGE_SIZE заведений) из результатов пользователя.
    Возвращает пару (текст, клавиатура).
    """
    t = TEXTS[lang]
    config = LISTS[group]
    title = t[config["title_key"]]

    rows = load_rows(config["file"], user_id) or []
    if not rows:
        keyboard = types.InlineKeyboardMarkup()
        keyboard.row(menu_button(t))
        return f"<b>{title}</b>\n\n" + esc(t["list_empty"]), keyboard

    pages = math.ceil(len(rows) / PAGE_SIZE)
    # Если запрошенной страницы нет (например, файл стал короче) — берём ближайшую
    page = max(0, min(page, pages - 1))

    start = page * PAGE_SIZE
    chunk = rows[start:start + PAGE_SIZE]

    header = f"<b>{title}</b>\n" + t["page_info"].format(page=page + 1, pages=pages, total=len(rows))
    # Нумерация сквозная: на 2-й странице записи идут с номера 11
    entries = [
        format_entry(number, row, config["show_site"], t)
        for number, row in enumerate(chunk, start=start + 1)
    ]
    text = header + "\n\n" + "\n\n".join(entries)

    return text, pagination_keyboard(group, page, pages, t)


# ---------------------------------------------------------------------------
# ОТПРАВКА И РЕДАКТИРОВАНИЕ СООБЩЕНИЙ
# ---------------------------------------------------------------------------

def send(chat_id, text, keyboard=None):
    """
    Отправляет НОВОЕ сообщение.
    parse_mode="HTML"  — включает жирный шрифт и ссылки;
    disable_web_page_preview=True — убирает «превью» сайтов, чтобы чат не пух.
    """
    bot.send_message(
        chat_id, text,
        parse_mode="HTML",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


def notify(chat_id, text):
    """
    Отправляет простое сообщение без разметки и НЕ падает, если отправить не вышло
    (например, пользователь заблокировал бота). Используется внутри фоновых потоков:
    там ошибка не должна оборвать работу и оставить пользователя «в поиске» навсегда.
    """
    try:
        bot.send_message(chat_id, text)
    except Exception:
        traceback.print_exc()


def edit(call, text, keyboard=None):
    """Заменяет текст и кнопки в уже существующем сообщении (так работает листание)."""
    try:
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except telebot.apihelper.ApiTelegramException as error:
        # Telegram ругается, если новый текст совпадает со старым (например, двойной тап).
        # Это не ошибка — просто игнорируем. Всё остальное пробрасываем дальше.
        if "message is not modified" not in str(error):
            raise


def get_lang_or_ask(user_id, chat_id):
    """
    Возвращает язык пользователя. Если он ещё не выбран (или бот перезапускался
    и забыл выбор) — показывает кнопки выбора языка и возвращает None.
    """
    lang = user_langs.get(user_id)
    if lang is None:
        bot.send_message(chat_id, CHOOSE_LANG_TEXT, reply_markup=language_keyboard())
    return lang


# ---------------------------------------------------------------------------
# ПОИСК (/search): РАЗБОР КОМАНДЫ И ФОНОВАЯ РАБОТА
# ---------------------------------------------------------------------------

def parse_search_args(text):
    """
    Разбирает текст команды «/search Реутов, улица Октября 44 1000».
    Возвращает (адрес, радиус) или None, если формат неверный.
    Радиус — последнее число в строке, всё остальное — адрес.
    """
    parts = (text or "").split(maxsplit=1)   # ["/search", "Реутов, ... 44 1000"]
    if len(parts) < 2:
        return None

    match = SEARCH_ARGS.fullmatch(parts[1].strip())
    if not match:
        return None

    address = " ".join(match.group(1).split())   # убираем лишние пробелы и переносы строк
    if not address or len(address) > 200:
        return None
    return address, int(match.group(2))


def safe_check(url):
    """
    Проверяет один сайт через check_website(). Любая неожиданная ошибка
    (например, кривой адрес в данных OSM) превращается в статус «не работает»,
    чтобы один плохой сайт не оборвал весь поиск.
    """
    try:
        return check_website(url)
    except Exception:
        return GROUP_ERROR, "Не работает (некорректный адрес)"


def classify_businesses(businesses):
    """
    Проверяет сайты всех заведений ПАРАЛЛЕЛЬНО (CHECK_WORKERS одновременно)
    и раскладывает заведения по трём группам. Порядок заведений сохраняется.
    """
    with ThreadPoolExecutor(max_workers=CHECK_WORKERS) as pool:
        results = list(pool.map(lambda business: safe_check(business["Сайт"]), businesses))

    groups = {GROUP_NO_SITE: [], GROUP_ERROR: [], GROUP_OK: []}
    for business, (group, status) in zip(businesses, results):
        business["Статус"] = status
        groups[group].append(business)
    return groups


def save_results(user_id, groups):
    """
    Сохраняет три CSV-файла в папку пользователя.
    Сначала пишем во временный файл, потом переименовываем — так бот никогда
    не прочитает наполовину записанный файл, даже если в этот момент листают список.
    """
    folder = user_dir(user_id)
    os.makedirs(folder, exist_ok=True)
    for group, items in groups.items():
        path = os.path.join(folder, OUTPUT_FILES[group])
        tmp_path = path + ".tmp"
        save_csv(items, tmp_path)
        os.replace(tmp_path, path)


def send_result_files(chat_id, user_id, groups, t):
    """Присылает пользователю CSV-файлы как документы (пустые группы пропускаем)."""
    captions = {GROUP_NO_SITE: "list_no_site", GROUP_ERROR: "list_error", GROUP_OK: "list_ok"}
    for group, items in groups.items():
        if not items:
            continue
        path = os.path.join(user_dir(user_id), OUTPUT_FILES[group])
        with open(path, "rb") as f:
            bot.send_document(chat_id, f, caption=f"{t[captions[group]]} — {len(items)}")


def run_search(chat_id, user_id, lang, address, radius):
    """
    Сам поиск. Выполняется в ОТДЕЛЬНОМ потоке, поэтому может спокойно работать
    минуты, пока бот отвечает другим пользователям.

    Шаги: адрес -> координаты -> поиск заведений -> проверка сайтов ->
    сохранение CSV -> отправка файлов -> статистика и кнопки.
    """
    t = TEXTS[lang]
    try:
        # 1. Геокодинг (по одному потоку за раз — правило Nominatim)
        with GEOCODE_LOCK:
            lat, lon = geocode(address)

        # 2. Поиск заведений в Overpass (не больше двух запросов одновременно)
        with OVERPASS_SLOTS:
            data = overpass_request(build_query(lat, lon, radius))
        businesses, _skipped = parse_elements(data.get("elements", []))

        if not businesses:
            notify(chat_id, t["search_empty"].format(address=address))
            return

        # Защита от гигантских выдач: берём первые MAX_BUSINESSES
        found_total = len(businesses)
        if found_total > MAX_BUSINESSES:
            businesses = businesses[:MAX_BUSINESSES]
            notify(chat_id, t["search_truncated"].format(found=found_total, checked=MAX_BUSINESSES))

        # 3. Проверка сайтов и разбивка на группы
        groups = classify_businesses(businesses)

        # 4. Сохраняем результаты пользователя и присылаем файлы
        save_results(user_id, groups)
        notify(chat_id, t["search_done"].format(address=address, radius=radius))
        send_result_files(chat_id, user_id, groups, t)

        # 5. Статистика и кнопки — как в /report
        text, keyboard = build_menu(lang, user_id)
        send(chat_id, text, keyboard)

    except SystemExit as stop:
        # Функции из find_businesses.py при ошибках вызывают sys.exit("текст ошибки").
        # В потоке это не завершило бы программу, а молча оборвало бы поиск —
        # поэтому перехватываем и показываем текст пользователю.
        notify(chat_id, t["search_failed"].format(reason=stop.code))
    except Exception:
        traceback.print_exc()   # подробности — в консоль, пользователю — короткое сообщение
        notify(chat_id, t["search_error"])
    finally:
        # В любом случае освобождаем пользователя, чтобы он мог запустить новый поиск
        with active_lock:
            active_users.discard(user_id)


# ---------------------------------------------------------------------------
# ОБРАБОТЧИКИ КОМАНД
# ---------------------------------------------------------------------------

@bot.message_handler(commands=["start"])
def handle_start(message):
    """Команда /start: показываем кнопки выбора языка."""
    bot.send_message(message.chat.id, CHOOSE_LANG_TEXT, reply_markup=language_keyboard())


@bot.message_handler(commands=["search"])
def handle_search(message):
    """Команда /search [адрес] [радиус]: проверяет ввод и запускает поиск в отдельном потоке."""
    user_id = message.from_user.id
    chat_id = message.chat.id

    lang = get_lang_or_ask(user_id, chat_id)
    if lang is None:
        return
    t = TEXTS[lang]

    # Разбираем «/search Реутов, улица Октября 44 1000»
    parsed = parse_search_args(message.text)
    if parsed is None:
        bot.send_message(chat_id, t["search_usage"])
        return
    address, radius = parsed

    if not MIN_RADIUS <= radius <= MAX_RADIUS:
        bot.send_message(chat_id, t["search_bad_radius"].format(min=MIN_RADIUS, max=MAX_RADIUS))
        return

    # «Записываем» пользователя как ищущего — под замком, потому что другие
    # потоки могут делать то же самое в эту же долю секунды
    with active_lock:
        if user_id in active_users:
            bot.send_message(chat_id, t["search_busy_user"])
            return
        if len(active_users) >= MAX_PARALLEL_SEARCHES:
            bot.send_message(chat_id, t["search_busy_server"])
            return
        active_users.add(user_id)

    try:
        # Сразу отвечаем пользователю и только потом запускаем долгую работу
        bot.send_message(chat_id, t["search_started"])
        threading.Thread(
            target=run_search,
            args=(chat_id, user_id, lang, address, radius),
            daemon=True,   # поток не будет мешать закрытию бота по Ctrl+C
        ).start()
    except BaseException:
        # Если поток не запустился — освобождаем пользователя, иначе он «застрянет»
        with active_lock:
            active_users.discard(user_id)
        raise


@bot.message_handler(commands=["report"])
def handle_report(message):
    """Команда /report: статистика и меню со списками."""
    lang = get_lang_or_ask(message.from_user.id, message.chat.id)
    if lang is None:
        return
    text, keyboard = build_menu(lang, message.from_user.id)
    send(message.chat.id, text, keyboard)


@bot.message_handler(commands=["nosite"])
def handle_nosite(message):
    """Команда /nosite: сразу открывает 1-ю страницу списка «Без сайта»."""
    lang = get_lang_or_ask(message.from_user.id, message.chat.id)
    if lang is None:
        return
    text, keyboard = build_page(lang, "no_site", 0, message.from_user.id)
    send(message.chat.id, text, keyboard)


@bot.message_handler(commands=["broken"])
def handle_broken(message):
    """Команда /broken: сразу открывает 1-ю страницу списка «Сломанные сайты»."""
    lang = get_lang_or_ask(message.from_user.id, message.chat.id)
    if lang is None:
        return
    text, keyboard = build_page(lang, "error_site", 0, message.from_user.id)
    send(message.chat.id, text, keyboard)


# ---------------------------------------------------------------------------
# ОБРАБОТЧИКИ НАЖАТИЙ НА КНОПКИ
# ---------------------------------------------------------------------------

@bot.callback_query_handler(func=lambda call: call.data in ("lang_ru", "lang_en"))
def handle_language_choice(call):
    """Кнопка языка: запоминаем выбор и подтверждаем его."""
    lang = call.data.split("_")[1]  # "lang_ru" -> "ru"
    user_langs[call.from_user.id] = lang

    # Обязательно «отвечаем» на нажатие, иначе на кнопке будут бесконечно крутиться часики
    bot.answer_callback_query(call.id)

    # Заменяем сообщение с кнопками на подтверждение (кнопки при этом исчезают)
    bot.edit_message_text(
        TEXTS[lang]["lang_set"],
        call.message.chat.id,
        call.message.message_id,
    )


@bot.callback_query_handler(func=lambda call: call.data == "noop")
def handle_noop(call):
    """Кнопка-счётчик страниц («2/25»): ничего не делаем, только снимаем «часики»."""
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "menu")
def handle_menu(call):
    """Кнопка «📊 Меню»: возвращаемся к статистике и выбору списка."""
    bot.answer_callback_query(call.id)
    lang = get_lang_or_ask(call.from_user.id, call.message.chat.id)
    if lang is None:
        return
    text, keyboard = build_menu(lang, call.from_user.id)
    edit(call, text, keyboard)


@bot.callback_query_handler(func=lambda call: call.data.startswith("page:"))
def handle_page(call):
    """Кнопки списка («Назад», «Вперёд», выбор списка в меню): показываем нужную страницу."""
    bot.answer_callback_query(call.id)

    # callback_data выглядит так: "page:no_site:3" -> группа и номер страницы (с нуля)
    try:
        _, group, page = call.data.split(":")
        page = int(page)
    except ValueError:
        return
    if group not in LISTS:
        return

    lang = get_lang_or_ask(call.from_user.id, call.message.chat.id)
    if lang is None:
        return

    text, keyboard = build_page(lang, group, page, call.from_user.id)
    edit(call, text, keyboard)


# ---------------------------------------------------------------------------
# ЗАПУСК
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Бот запущен. Для остановки нажмите Ctrl+C.")
    # infinity_polling() постоянно спрашивает у Telegram о новых сообщениях
    # и сам перезапускается, если пропал интернет или произошла ошибка.
    bot.infinity_polling()
