# RTN-170 Parser

Автономный возобновляемый сборщик реестра действующих лицензий Ростехнадзора
по 170-ФЗ с официального сайта
[gosnadzor.ru](https://www.gosnadzor.ru/service/list/reestr_licences_170fz/).
Данные сохраняются в отдельную схему PostgreSQL `rtn170_shared`.

## Возможности

- извлечение всех полей карточки лицензии, адресов и видов работ;
- строгая проверка активного статуса без совпадений по подстроке;
- контрольные точки, атомарные записи и аудит пропущенных карточек;
- проверка неизменности первой страницы при возобновлении;
- необязательные резервные маршруты ProxyHarbor и Best-Proxies;
- проверка каждого прокси на официальном домене с обязательным доверенным TLS;
- офлайн-тесты парсинга и опциональные тесты на отдельной PostgreSQL.

## Быстрый старт

Требуются Python 3.10+ и PostgreSQL 14+.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
export RTN_DATABASE_URL='postgresql://user:password@127.0.0.1/rtn170'
.venv/bin/python rtn170_parser.py init-db
.venv/bin/python rtn170_parser.py check-source
.venv/bin/python rtn170_parser.py crawl --delay 3
```

Для продолжения существующего прохода используйте UUID из команды `status`:

```bash
.venv/bin/python rtn170_parser.py status
.venv/bin/python rtn170_parser.py crawl --resume RUN_UUID --delay 3
```

Прокси настраиваются только через окружение; реальные ключи нельзя сохранять
в репозитории:

```bash
export PROXYHARBOR_API_TOKEN='...'
export BEST_PROXIES_API_KEY='...'
```

## Тесты

```bash
pip install -r requirements-dev.txt
pytest -q
```

Интеграционные тесты выполняются только с одноразовой БД, имя которой содержит
`rtn170_test`, через `RTN_TEST_DATABASE_URL`.

## Ограничения

Сборщик не обходит CAPTCHA и не отключает проверку TLS. Ответы 403/429,
изменение структуры и запрет `robots.txt` приводят к безопасной остановке либо
смене уже настроенного маршрута. Соблюдайте правила и допустимую нагрузку
официального источника.
