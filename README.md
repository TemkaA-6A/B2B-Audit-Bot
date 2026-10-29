# B2B Audit Bot 🤖

Telegram-бот и Python-скрипт для автоматического поиска малого бизнеса и аудита их сайтов. 

## 🚀 Возможности
- Поиск бизнесов (кафе, магазины, фитнес-центры) в заданном радиусе от адреса через OpenStreetMap.
- Проверка наличия и работоспособности сайтов у найденных заведений.
- Вывод отчетов и списков клиентов прямо в Telegram (с пагинацией и ссылками на Яндекс.Карты).

## 📦 Установка
1. Установите зависимости: `pip install -r requirements.txt` (или `pip install requests pyTelegramBotAPI`)
2. Создайте бота в @BotFather и вставьте токен в `tg_bot.py`.
3. Запустите бота: `python tg_bot.py`

## 🛠 Стек
Python, OpenStreetMap API (Nominatim, Overpass), pyTelegramBotAPI.
