#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
find_businesses.py — поиск малого бизнеса вокруг адреса через OpenStreetMap
и сортировка найденных заведений по «температуре» клиента.

Используются только бесплатные сервисы:
  * Nominatim — превращает адрес в координаты (геокодинг);
  * Overpass API — ищет объекты на карте в заданном радиусе.

Результат раскладывается по трём файлам:
  * no_site.csv    — ГОРЯЧИЕ: сайта нет совсем (продаём сайт с нуля);
  * error_site.csv — ТЁПЛЫЕ: сайт есть, но не работает (продаём срочный ремонт);
  * has_site.csv   — ХОЛОДНЫЕ: сайт работает (откладываем, возможен редизайн).

Установка:   pip install requests
Запуск:      python find_businesses.py "Тверская 1, Москва" 500
             (или просто python find_businesses.py — скрипт спросит сам)
"""

import argparse
import csv
import sys
import time

import requests

# ---------------------------------------------------------------------------
# НАСТРОЙКИ
# ---------------------------------------------------------------------------

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Без User-Agent сервисы OSM блокируют запросы, поэтому он обязателен
HEADERS = {"User-Agent": "MyBusinessAuditApp/1.0"}

# Названия групп клиентов (внутренние ключи) и файлов, куда они сохраняются
GROUP_NO_SITE = "no_site"      # горячие
GROUP_ERROR = "error_site"     # тёплые
GROUP_OK = "has_site"          # холодные

OUTPUT_FILES = {
    GROUP_NO_SITE: "no_site.csv",
    GROUP_ERROR: "error_site.csv",
    GROUP_OK: "has_site.csv",
}

OVERPASS_QUERY_TIMEOUT = 60  # сколько секунд сервер Overpass может считать запрос
MAX_RETRIES = 5              # сколько раз повторяем запрос при 429/504 и т.п.
RETRY_BASE_DELAY = 10        # базовая пауза между попытками (растёт: 10, 20, 30...)
SITE_TIMEOUT = 5             # таймаут при проверке сайта заведения (секунды)

# Пропускать ли объекты без названия (в OSM много безымянных точек,
# для аудита они бесполезны). Поставьте False, чтобы оставить их.
SKIP_UNNAMED = True


# ---------------------------------------------------------------------------
# ШАГ 1. ГЕОКОДИНГ (адрес -> координаты)
# ---------------------------------------------------------------------------

def geocode(address):
    """Спрашивает у Nominatim координаты адреса. Возвращает (широта, долгота)."""
    params = {"q": address, "format": "json", "limit": 1}
    try:
        response = requests.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=15)
        response.raise_for_status()  # выбросит ошибку, если код ответа 4xx/5xx
        data = response.json()
    except (requests.exceptions.RequestException, ValueError) as error:
        sys.exit(f"Ошибка геокодинга: {error}")
    finally:
        # Правила Nominatim: не чаще 1 запроса в секунду
        time.sleep(1)

    if not data:
        sys.exit(f"Адрес не найден: {address!r}. Попробуйте записать его подробнее.")

    # Nominatim возвращает координаты строками — переводим в числа
    return float(data[0]["lat"]), float(data[0]["lon"])


# ---------------------------------------------------------------------------
# ШАГ 2. ПОИСК ОБЪЕКТОВ ЧЕРЕЗ OVERPASS
# ---------------------------------------------------------------------------

def build_query(lat, lon, radius):
    """
    Собирает запрос на языке Overpass QL.
      nwr  = node + way + relation (точки, здания и т.д.)
      around:радиус,широта,долгота — «в радиусе от точки»
    Барбершопы в OSM — это обычно shop=hairdresser, поэтому они попадают
    под условие shop=*.
    `out center tags` — вернуть теги И координаты: у точек (node) они приходят
    сами, а для зданий и областей (way/relation) Overpass считает «центр».
    """
    return f"""
    [out:json][timeout:{OVERPASS_QUERY_TIMEOUT}];
    (
      nwr["amenity"="cafe"](around:{radius},{lat},{lon});
      nwr["shop"](around:{radius},{lat},{lon});
      nwr["leisure"="fitness_centre"](around:{radius},{lat},{lon});
    );
    out center tags;
    """


def overpass_request(query):
    """
    Отправляет запрос в Overpass. Если сервер перегружен (429, 502, 503, 504)
    или пропала связь — ждёт и пробует снова, до MAX_RETRIES раз.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        wait = RETRY_BASE_DELAY * attempt  # пауза растёт с каждой попыткой
        try:
            response = requests.post(
                OVERPASS_URL,
                data={"data": query},
                headers=HEADERS,
                timeout=OVERPASS_QUERY_TIMEOUT + 30,
            )
        except requests.exceptions.RequestException as error:
            print(f"  Сетевая ошибка: {error}")
        else:
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    # Иногда сервер отвечает 200, но вместо JSON присылает HTML с ошибкой
                    print("  Overpass вернул некорректный ответ")
            elif response.status_code in (429, 502, 503, 504):
                print(f"  Overpass перегружен (код {response.status_code})")
                # Если сервер сам сказал, сколько ждать — слушаемся
                retry_after = response.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    wait = int(retry_after)
            else:
                sys.exit(f"Overpass вернул неожиданный код {response.status_code}")

        if attempt < MAX_RETRIES:
            print(f"  Жду {wait} сек и пробую снова (попытка {attempt + 1} из {MAX_RETRIES})...")
            time.sleep(wait)

    sys.exit("Overpass не ответил после нескольких попыток. Попробуйте позже.")


# ---------------------------------------------------------------------------
# ШАГ 3. РАЗБОР ДАННЫХ
# ---------------------------------------------------------------------------

def parse_elements(elements):
    """Достаёт из ответа Overpass название, адрес и сайт каждого объекта."""
    businesses = []
    skipped = 0

    for element in elements:
        tags = element.get("tags", {})  # все теги объекта: name, website и т.д.
        name = tags.get("name")

        if not name and SKIP_UNNAMED:
            skipped += 1
            continue

        # Адрес собираем из улицы и номера дома (если их нет — остаётся пустая строка;
        # бот в этом случае покажет координаты и ссылку на Яндекс.Карты)
        street = tags.get("addr:street", "")
        house = tags.get("addr:housenumber", "")
        address = f"{street} {house}".strip()

        # Координаты: у точек (node) lat/lon лежат прямо в объекте,
        # у зданий и областей (way/relation) — внутри "center"
        center = element.get("center", {})
        lat = element.get("lat", center.get("lat"))
        lon = element.get("lon", center.get("lon"))

        # Сайт: основной тег website, запасной — contact:website.
        # strip() убирает пробелы: тег из одних пробелов считаем пустым.
        website = (tags.get("website") or tags.get("contact:website") or "").strip()

        businesses.append({
            "Название": name or "Без названия",
            "Адрес": address,
            "Сайт": website,
            "Статус": "",
            "Широта": lat if lat is not None else "",
            "Долгота": lon if lon is not None else "",
        })

    return businesses, skipped


# ---------------------------------------------------------------------------
# ШАГ 4. ПРОВЕРКА САЙТА И ОПРЕДЕЛЕНИЕ ГРУППЫ
# ---------------------------------------------------------------------------

def check_website(url):
    """
    Проверяет сайт и возвращает пару (группа, текст статуса).
      * нет сайта            -> (GROUP_NO_SITE, "Нет сайта")
      * код 200              -> (GROUP_OK,      "Работает")
      * ошибка / таймаут /
        404 / 500 и т.д.     -> (GROUP_ERROR,   "Не работает (причина)")
    """
    if not url:
        return GROUP_NO_SITE, "Нет сайта"

    # В OSM сайт иногда записан без http:// — добавляем, иначе requests не поймёт адрес
    if not url.lower().startswith(("http://", "https://")):
        url = "http://" + url

    try:
        # stream=True — не скачиваем всю страницу целиком, нам нужен только код ответа.
        # Конструкция with сама закроет соединение после проверки.
        with requests.get(url, headers=HEADERS, timeout=SITE_TIMEOUT, stream=True) as response:
            code = response.status_code
    except requests.exceptions.Timeout:
        return GROUP_ERROR, "Не работает (таймаут)"
    except requests.exceptions.RequestException:
        return GROUP_ERROR, "Не работает (ошибка соединения)"

    if code == 200:
        return GROUP_OK, "Работает"
    return GROUP_ERROR, f"Не работает (код {code})"


# ---------------------------------------------------------------------------
# СОХРАНЕНИЕ И ГЛАВНАЯ ФУНКЦИЯ
# ---------------------------------------------------------------------------

def save_csv(businesses, path):
    """Сохраняет список заведений в CSV (utf-8-sig, чтобы Excel видел русские буквы)."""
    columns = ["Название", "Адрес", "Сайт", "Статус", "Широта", "Долгота"]
    # Сортируем по названию, чтобы список было удобно листать
    businesses = sorted(businesses, key=lambda b: b["Название"].casefold())
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(businesses)


def get_input():
    """Получает адрес и радиус: из командной строки или спрашивает у пользователя."""
    parser = argparse.ArgumentParser(description="Поиск малого бизнеса через OpenStreetMap")
    parser.add_argument("address", nargs="?", help="адрес, вокруг которого искать")
    parser.add_argument("radius", nargs="?", type=int, help="радиус поиска в метрах")
    args = parser.parse_args()

    address = args.address or input("Введите адрес: ").strip()
    radius = args.radius
    if radius is None:
        try:
            radius = int(input("Введите радиус в метрах: ").strip())
        except ValueError:
            sys.exit("Радиус должен быть целым числом.")

    if not address or radius <= 0:
        sys.exit("Нужен непустой адрес и положительный радиус.")
    return address, radius


def main():
    address, radius = get_input()

    print(f"Ищу координаты: {address}")
    lat, lon = geocode(address)

    print(f"Ищу заведения в радиусе {radius} м...")
    data = overpass_request(build_query(lat, lon, radius))
    elements = data.get("elements", [])
    businesses, skipped = parse_elements(elements)

    print(f"Найдено объектов: {len(elements)} (без названия пропущено: {skipped})")
    if not businesses:
        sys.exit("Ничего не найдено. Попробуйте увеличить радиус.")

    # Сюда будем раскладывать заведения по трём группам
    groups = {GROUP_NO_SITE: [], GROUP_ERROR: [], GROUP_OK: []}
    total = len(businesses)

    print(f"Проверяю сайты ({total} шт.):\n")
    for number, business in enumerate(businesses, start=1):
        group, status = check_website(business["Сайт"])
        business["Статус"] = status
        groups[group].append(business)

        # Во время проверки выводим только название и статус
        print(f"[{number}/{total}] {business['Название']}: {status}")

    # Сохраняем каждую группу в свой файл
    for group, items in groups.items():
        save_csv(items, OUTPUT_FILES[group])

    # Итоговая статистика
    print("\n" + "=" * 70)
    print(
        f"Всего найдено: {total} | "
        f"Без сайта: {len(groups[GROUP_NO_SITE])} | "
        f"Сайт сломан: {len(groups[GROUP_ERROR])} | "
        f"Сайт работает: {len(groups[GROUP_OK])}"
    )
    print("=" * 70)
    print(f"Горячие клиенты (нет сайта):  {OUTPUT_FILES[GROUP_NO_SITE]}")
    print(f"Тёплые клиенты (сайт сломан): {OUTPUT_FILES[GROUP_ERROR]}")
    print(f"Холодные клиенты (всё ок):    {OUTPUT_FILES[GROUP_OK]}")


# Запускаем main() только при прямом запуске файла
if __name__ == "__main__":
    main()
