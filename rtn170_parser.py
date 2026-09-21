#!/usr/bin/env python3
"""Standalone Rostekhnadzor 170-FZ active-license collector (Python 3.10+).

No application imports, embedded credentials or external table dependencies.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import contextlib
import datetime as dt
import email.utils
import gzip
import hashlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import random
import re
import signal
import sys
import time
import unicodedata
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
import uuid

from bs4 import BeautifulSoup
import requests

# Transport fallback changes do not alter the persisted parser/schema contract;
# keep this compatible with resumable 1.0.1 runs already stored in PostgreSQL.
VERSION = "1.0.1"
SCHEMA = "rtn170_shared"
BASE = "https://www.gosnadzor.ru/service/list/reestr_licences_170fz/"
SOURCE = "gosnadzor_170fz"
AGENT = "Rtn170RegistryCollector"
LOCK = 17020260918
LOG = logging.getLogger(AGENT)
NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, BASE)
PROXYHARBOR_API = "https://proxy.blagodaty.ru/api/v1/proxies"
BEST_PROXIES_API = "https://api.best-proxies.ru/proxylist.json"
PROXY_PROBE_URL = "https://www.gosnadzor.ru/robots.txt"

ORG_FIELDS = {
    "полное наименование организации": "full_name",
    "сокращенное наименование организации": "short_name",
    "адрес": "address",
    "инн": "inn",
    "огрн": "ogrn",
}
LIC_FIELDS = {
    "номер лицензии": "license_number",
    "дата регистрации лицензии": "registered_on_raw",
    "срок действия лицензии": "valid_until_raw",
    "лицензируемый вид деятельности": "activity",
    "виды работ (услуг), выполняемых (оказываемых) в составе лицензируемого вида деятельности": "work_types_text",
    "адреса мест осуществления лицензируемого вида деятельности": "activity_addresses_text",
    "статус": "source_status",
}
TEXT_FIELDS = ([f"{s}_{f}" for s in ("licensee", "object") for f in ORG_FIELDS.values()]
               + list(LIC_FIELDS.values()))


class ParseError(RuntimeError):
    pass


class SourceBlocked(RuntimeError):
    pass


class FetchError(RuntimeError):
    pass


class ConfigError(RuntimeError):
    pass


class ProxyHarborPool:
    """Find an HTTPS-capable route without ever logging the API token.

    ProxyHarbor validates general connectivity, while this collector must also
    verify that the specific official source accepts the proxy and presents a
    trusted TLS certificate.  Only HTTP CONNECT proxies are used here so the
    base dependency set does not need PySocks.
    """

    def __init__(self, token: str, max_latency_ms: int = 20000, probe_limit: int = 400):
        self.token = token
        self.max_latency_ms = max(1000, max_latency_ms)
        self.probe_limit = max(1, probe_limit)
        self._candidates: list[str] | None = None
        self._rejected: set[str] = set()

    @classmethod
    def from_environment(cls):
        token = os.environ.get("PROXYHARBOR_API_TOKEN", "").strip()
        if not token:
            return None
        try:
            max_latency = int(os.environ.get("PROXYHARBOR_MAX_LATENCY_MS", "20000"))
            probe_limit = int(os.environ.get("PROXYHARBOR_PROBE_LIMIT", "400"))
        except ValueError as exc:
            raise ConfigError("Некорректные числовые параметры ProxyHarbor") from exc
        return cls(token, max_latency, probe_limit)

    @staticmethod
    def _validated_proxy_url(item: dict) -> str | None:
        if str(item.get("protocol", "")).casefold() not in {"http", "https"}:
            return None
        try:
            host = str(ipaddress.ip_address(str(item["host"])))
            port = int(item["port"])
        except (KeyError, ValueError):
            return None
        if not 1 <= port <= 65535:
            return None
        return f"http://{host}:{port}"

    def candidates(self) -> list[str]:
        if self._candidates is not None:
            return self._candidates
        session = requests.Session()
        session.trust_env = False
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        items: list[dict] = []
        try:
            for page in range(1, 11):
                response = session.get(
                    PROXYHARBOR_API,
                    headers=headers,
                    params={"page": page, "pageSize": 200,
                            "maxLatencyMs": self.max_latency_ms},
                    timeout=(10, 30),
                )
                if response.status_code in (401, 403):
                    raise ConfigError("ProxyHarbor отклонил API-токен")
                response.raise_for_status()
                payload = response.json()
                page_items = payload.get("items") if isinstance(payload, dict) else None
                if not isinstance(page_items, list):
                    raise FetchError("ProxyHarbor вернул неожиданный формат каталога")
                items.extend(x for x in page_items if isinstance(x, dict))
                total = int(payload.get("total", len(items)))
                if len(items) >= total or not page_items:
                    break
        except requests.RequestException as exc:
            raise FetchError("Не удалось получить каталог ProxyHarbor") from exc
        finally:
            session.close()
        items.sort(key=lambda x: (-float(x.get("successRate") or 0),
                                  int(x.get("latencyMs") or 10**9)))
        seen = set()
        candidates = []
        for item in items:
            proxy = self._validated_proxy_url(item)
            if proxy and proxy not in seen:
                seen.add(proxy)
                candidates.append(proxy)
                if len(candidates) >= self.probe_limit:
                    break
        self._candidates = candidates
        return candidates

    @staticmethod
    def _probe(proxy: str) -> str | None:
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.get(
                PROXY_PROBE_URL,
                headers={"User-Agent": f"{AGENT}/{VERSION}", "Accept": "text/plain"},
                proxies={"http": proxy, "https": proxy},
                timeout=(7, 12),
                allow_redirects=False,
            )
            if response.status_code != 200 or not response.content:
                return None
            if len(response.content) > 512 * 1024:
                return None
            text = response.text
            if re.search(r"<\s*(?:html|form|script)\b", text, re.I):
                return None
            return proxy
        except requests.RequestException:
            return None
        finally:
            session.close()

    def select(self) -> str:
        candidates = [x for x in self.candidates() if x not in self._rejected]
        if not candidates:
            raise FetchError("ProxyHarbor: нет непроверенных HTTP(S)-прокси")
        executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="proxy-probe")
        futures = {executor.submit(self._probe, proxy): proxy for proxy in candidates}
        try:
            for future in as_completed(futures):
                result = future.result()
                if result:
                    for pending in futures:
                        if pending is not future:
                            pending.cancel()
                    return result
                self._rejected.add(futures[future])
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        raise FetchError("ProxyHarbor: официальный источник отклонил все проверенные маршруты")

    def reject(self, proxy: str | None):
        if proxy:
            self._rejected.add(proxy)


class BestProxiesPool(ProxyHarborPool):
    """Best-Proxies catalog with the same strict per-source verification."""

    @classmethod
    def from_environment(cls):
        token = os.environ.get("BEST_PROXIES_API_KEY", "").strip()
        if not token:
            return None
        try:
            probe_limit = int(os.environ.get("BEST_PROXIES_PROBE_LIMIT", "400"))
        except ValueError as exc:
            raise ConfigError("Некорректный BEST_PROXIES_PROBE_LIMIT") from exc
        return cls(token, 20000, probe_limit)

    def candidates(self) -> list[str]:
        if self._candidates is not None:
            return self._candidates
        session = requests.Session()
        session.trust_env = False
        try:
            response = session.get(
                BEST_PROXIES_API,
                params={
                    "key": self.token,
                    "limit": self.probe_limit,
                    "type": "http,https",
                    "level": "1,2",
                    "bl": "3",
                    "nocascade": "1",
                },
                timeout=(10, 45),
            )
            if response.status_code in (401, 403):
                raise ConfigError("Best-Proxies отклонил API-ключ")
            if response.status_code == 429:
                raise FetchError("Best-Proxies: превышен лимит API")
            response.raise_for_status()
            items = response.json()
            if not isinstance(items, list):
                raise FetchError("Best-Proxies вернул неожиданный формат каталога")
        except (requests.RequestException, ValueError) as exc:
            raise FetchError("Не удалось получить каталог Best-Proxies") from exc
        finally:
            session.close()
        seen = set()
        candidates = []
        for item in items:
            if not isinstance(item, dict) or not (item.get("http") == 1 or item.get("https") == 1):
                continue
            proxy = self._validated_proxy_url({
                "protocol": "http", "host": item.get("ip"), "port": item.get("port")
            })
            if proxy and proxy not in seen:
                seen.add(proxy)
                candidates.append(proxy)
        self._candidates = candidates
        return candidates


class CompositeProxyPool:
    def __init__(self, pools):
        self.pools = pools

    def select(self) -> str:
        for pool in self.pools:
            try:
                return pool.select()
            except FetchError:
                continue
        raise FetchError("Ни один настроенный прокси-сервис не дал рабочего маршрута к источнику")

    def reject(self, proxy: str | None):
        for pool in self.pools:
            pool.reject(proxy)


def proxy_pool_from_environment():
    # Try the most recently purchased/fresh catalog first, then ProxyHarbor.
    pools = [x for x in (BestProxiesPool.from_environment(),
                         ProxyHarborPool.from_environment()) if x]
    if not pools:
        return None
    return pools[0] if len(pools) == 1 else CompositeProxyPool(pools)


def norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return re.sub(r"\s+", " ", value).strip().rstrip(":").strip().casefold()


def compact(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def number_key(value: str) -> str:
    # Never conflate Latin and Cyrillic letters or remove meaningful punctuation.
    return compact(unicodedata.normalize("NFKC", value)).upper()


def is_active(value: str) -> bool:
    # Substring matching would wrongly accept 'Недействующая'.
    # The source also uses "Возобновлена" for a licence whose suspension has
    # ended.  Match only the explicit source vocabulary so that a new or
    # ambiguous status is still handled conservatively.
    return norm(value) in {"действующая", "возобновлена"}


def digest(value) -> str:
    if not isinstance(value, bytes):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def cell_text(cell) -> str:
    return compact(cell.get_text(" ", strip=True))


def items_from_cell(cell) -> list[str]:
    lis = [x for x in cell.find_all("li") if x.find_parent("li") is None]
    if lis:
        return [cell_text(x) for x in lis if cell_text(x)]
    blocks = cell.find_all(["p", "div"], recursive=False)
    if blocks:
        return [cell_text(x) for x in blocks if cell_text(x)]
    # Preserve address commas/semicolons: these are not reliable item separators.
    clone = BeautifulSoup(str(cell), "html.parser")
    for br in clone.find_all("br"):
        br.replace_with("\n")
    return [compact(x) for x in clone.get_text().splitlines() if compact(x)]


def section_name(text: str):
    key = norm(text).replace("–", "-").replace("—", "-")
    key = re.sub(r"\s*-\s*", "-", key)
    return {"организация-лицензиат": "licensee", "организация-объект": "object",
            "информация о лицензии": "license"}.get(key)


def parse_date(value: str, label: str, warnings: list[str]):
    value = compact(value)
    if not value or value in ("-", "—"):
        return None
    if re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", value):
        try:
            return dt.datetime.strptime(value, "%d.%m.%Y").date().isoformat()
        except ValueError:
            pass
    warnings.append(f"{label}: дата не преобразована, исходный текст сохранен")
    return None


def parse_card(html: str, expected_number: str | None = None) -> dict:
    """Label + section parser; no dependency on CSS/row positions of the card."""
    soup = BeautifulSoup(html, "html.parser")
    section = None
    values: dict[str, str] = {}
    raw_fields = []
    work_items, address_items, warnings = [], [], []
    # Headers can be table rows or headings. Ignore enclosing layout table rows.
    for node in soup.find_all(["tr", "h1", "h2", "h3", "h4"]):
        if node.name != "tr":
            section = section_name(cell_text(node)) or section
            continue
        cells = node.find_all(["td", "th"], recursive=False)
        if not cells or any(c.find("table") for c in cells):
            continue
        if len(cells) == 1:
            section = section_name(cell_text(cells[0])) or section
            continue
        label = cell_text(cells[0])
        if not section or not label:
            continue
        if len(cells) != 2:
            raise ParseError("Неожиданная многоколоночная строка карточки")
        value = cell_text(cells[1])
        field = (ORG_FIELDS.get(norm(label)) if section != "license"
                 else LIC_FIELDS.get(norm(label)))
        if field and section != "license":
            field = f"{section}_{field}"
        raw_fields.append({"ordinal": len(raw_fields) + 1, "section": section,
                           "label": label, "value_text": value,
                           "value_html": cells[1].decode_contents(), "mapped_column": field})
        if not field:
            warnings.append(f"Новое поле: {section}/{label}; сохранено отдельно")
            continue
        if field in values:
            raise ParseError(f"Повтор поля {field}: нужна проверка структуры карточки")
        values[field] = value
        if field == "work_types_text":
            work_items = items_from_cell(cells[1])
        elif field == "activity_addresses_text":
            address_items = items_from_cell(cells[1])
    missing = set(TEXT_FIELDS) - set(values)
    if missing:
        raise ParseError("Отсутствуют подписи полей: " + ", ".join(sorted(missing)))
    for required in ("license_number", "source_status", "licensee_full_name"):
        if not values[required]:
            raise ParseError(f"Пустое обязательное значение {required}")
    values["license_number"] = number_key(values["license_number"])
    if expected_number and values["license_number"] != number_key(expected_number):
        raise ParseError("Номер карточки не совпадает с номером в ссылке")
    for prefix in ("licensee", "object"):
        for name, lengths in (("inn", (10, 12)), ("ogrn", (13, 15))):
            val = values[f"{prefix}_{name}"]
            if val and not (val.isascii() and val.isdigit() and len(val) in lengths):
                warnings.append(f"{prefix}_{name}: нестандартное значение сохранено без изменения")
    indefinite = norm(values["valid_until_raw"]) in ("бессрочно", "бессрочная", "бессрочный")
    values["registered_on"] = parse_date(values["registered_on_raw"], "registered_on", warnings)
    values["valid_until"] = None if indefinite else parse_date(values["valid_until_raw"], "valid_until", warnings)
    values["valid_indefinite"] = indefinite
    return {"fields": values, "work_items": work_items, "address_items": address_items,
            "raw_fields": raw_fields, "warnings": warnings}


def safe_url(url: str) -> str:
    u = urlsplit(url)
    b = urlsplit(BASE)
    if (u.scheme != "https" or u.netloc != b.netloc or u.username or u.password
            or (not u.path.startswith(b.path) and u.path != "/robots.txt")):
        raise SourceBlocked("Источник предложил URL вне разрешенного HTTPS-раздела")
    return url


def detail_url(href: str) -> str:
    url = safe_url(urljoin(BASE, href))
    u = urlsplit(url)
    q = parse_qs(u.query)
    if u.path != urlsplit(BASE).path + "license.php" or len(q.get("licNum", [])) != 1:
        raise ParseError("Неподдерживаемая ссылка карточки")
    return urlunsplit((u.scheme, u.netloc, u.path,
                       urlencode({"licNum": q["licNum"][0], "ajax": "1"}), ""))


def parse_list(html: str, page: int = 1, require_active_filter: bool = True) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table#licenses_list")
    form = soup.select_one("form#licenses_form")
    if table is None or form is None:
        raise ParseError("Нет таблицы/формы реестра; это не пустой результат")
    checkbox = form.select_one('input[name="active"]')
    if require_active_filter and (checkbox is None or not checkbox.has_attr("checked")):
        raise ParseError("Сервер не подтвердил фильтр действующих лицензий")
    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td", recursive=False)
        if not cells:
            continue
        if len(cells) != 6:
            raise ParseError("Изменились колонки списка лицензий")
        a = cells[0].find("a", href=True)
        if a is None:
            raise ParseError("Нет ссылки на карточку в строке")
        num, status = number_key(cell_text(a)), cell_text(cells[5])
        url = detail_url(a["href"])
        if number_key(parse_qs(urlsplit(url).query)["licNum"][0]) != num:
            raise ParseError("Номер в ссылке не совпадает с номером строки")
        rows.append({"number": num, "url": url, "status": status})
    # Restrict counter parsing to the content after the registry table.
    after = " ".join(cell_text(x) for x in table.next_siblings if getattr(x, "name", None))
    m = re.search(r"(\d+)\s*[-–—]\s*(\d+)\s+из\s+(\d[\d \u00a0]*\d|\d)", after)
    if not m:
        # Fail closed for a changed or zero-result layout until it is reviewed.
        raise ParseError("Не найден подтвержденный счетчик 'N - M из K'")
    start, end, total = (int(re.sub(r"\s", "", v)) for v in m.groups())
    if not rows or end - start + 1 != len(rows) or not 1 <= start <= end <= total:
        raise ParseError("Счетчик не совпадает со строками списка")
    if page == 1 and start != 1:
        raise ParseError("Получена не первая страница")
    active_rows = [r for r in rows if is_active(r["status"])]
    pages = [page]
    for a in soup.find_all("a", href=True):
        query = parse_qs(urlsplit(urljoin(BASE, a["href"])).query)
        for p in query.get("PAGEN_1", []):
            if p.isdigit():
                pages.append(int(p))
    return {"rows": rows, "all_row_count": len(rows), "inactive_row_count": len(rows) - len(active_rows),
            "start": start, "end": end,
            "total": total, "last_page_link": max(pages),
            "signature": digest([r["number"] for r in rows])}


class Client:
    def __init__(self, delay: float = 3.0):
        # One sequential request per second is the fastest supported mode.
        # robots.txt can still raise this floor for the active route.
        self.delay = max(1.0, delay)
        self.last_request = 0.0
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": f"{AGENT}/{VERSION}",
                                     "Accept": "text/html", "Accept-Language": "ru"})
        self.robots = None
        self.proxy_pool = proxy_pool_from_environment()
        self.proxy_url = None

    def close(self):
        self.session.close()

    def get(self, url: str, robots_check: bool = True) -> str:
        safe_url(url)
        if robots_check and self.robots and not self.robots.can_fetch(AGENT, url):
            raise SourceBlocked("robots.txt запрещает выбранный URL")
        for attempt in range(4):
            time.sleep(max(0.0, self.last_request + self.delay - time.monotonic()))
            self.last_request = time.monotonic()
            try:
                proxies = ({"http": self.proxy_url, "https": self.proxy_url}
                           if self.proxy_url else None)
                with self.session.get(url, timeout=(15, 60), allow_redirects=False,
                                      stream=True, proxies=proxies) as resp:
                    if resp.status_code in (401, 403):
                        if self.proxy_pool:
                            self.proxy_pool.reject(self.proxy_url)
                            self.proxy_url = self.proxy_pool.select()
                            continue
                        raise SourceBlocked(f"HTTP {resp.status_code}; обход защиты не предусмотрен")
                    if 300 <= resp.status_code < 400:
                        raise SourceBlocked("Редирект требует ручной проверки; автоматически не переходить")
                    if url.endswith("/robots.txt") and resp.status_code in (404, 410):
                        return "User-agent: *\nAllow: /\n"
                    if resp.status_code == 429:
                        raise SourceBlocked("HTTP 429; остановка до разрешенного повторного запуска")
                    if resp.status_code >= 500:
                        if attempt == 3:
                            raise FetchError("Повторяющаяся ошибка сервера 5xx")
                        retry = resp.headers.get("Retry-After", "")
                        wait = 2 ** (attempt + 2) + random.random()
                        if retry.isdigit():
                            wait = max(wait, int(retry))
                        elif retry:
                            try:
                                until = email.utils.parsedate_to_datetime(retry)
                                wait = max(wait, (until - dt.datetime.now(dt.timezone.utc)).total_seconds())
                            except (ValueError, TypeError):
                                pass
                        if wait > 60:
                            raise FetchError("Сервер требует длительную паузу; повторите запуск позднее")
                        time.sleep(wait)
                        continue
                    if resp.status_code != 200:
                        raise FetchError(f"HTTP {resp.status_code}")
                    body = bytearray()
                    for chunk in resp.iter_content(65536):
                        body.extend(chunk)
                        if len(body) > 8 * 1024 * 1024:
                            raise FetchError("Ответ превышает 8 MiB")
                    # Honor explicit charset; otherwise let HTML meta/UnicodeDammit decode.
                    charset = re.search(r"charset\s*=\s*([^; ]+)", resp.headers.get("Content-Type", ""), re.I)
                    if charset:
                        text = bytes(body).decode(charset[1].strip("\"'"), errors="strict")
                    else:
                        from bs4 import UnicodeDammit
                        text = UnicodeDammit(bytes(body)).unicode_markup
                    if not text:
                        # The registry occasionally closes a successful-looking
                        # response without a body.  Treat it as a transient
                        # transport failure, but keep the same bounded retry
                        # policy as 5xx responses.
                        if attempt == 3:
                            raise FetchError("Пустое тело ответа после четырех попыток")
                        time.sleep(2 ** (attempt + 2) + random.random())
                        continue
                    if url.endswith("/robots.txt") and re.search(r"<\s*(?:html|form|script)\b", text, re.I):
                        raise SourceBlocked("Вместо robots.txt возвращен HTML; требуется ручная проверка")
                    visible = norm(BeautifulSoup(text, "html.parser").get_text(" "))
                    if any(x in visible for x in ("verify you are human", "checking your browser",
                                                  "проверка браузера", "подтвердите, что вы не робот")):
                        raise SourceBlocked("Получена проверка браузера/робота; требуется оператор")
                    return text
            except requests.RequestException:
                if self.proxy_pool and not self.proxy_url:
                    self.proxy_url = self.proxy_pool.select()
                    continue
                if self.proxy_pool and self.proxy_url:
                    self.proxy_pool.reject(self.proxy_url)
                    self.proxy_url = self.proxy_pool.select()
                    continue
                if attempt == 3:
                    raise FetchError("Сетевая ошибка после четырех попыток") from None
                time.sleep(2 ** (attempt + 2) + random.random())
        raise FetchError("Не удалось получить ответ")

    def load_robots(self):
        robots_url = "https://www.gosnadzor.ru/robots.txt"
        text = self.get(robots_url, robots_check=False)
        self.robots = RobotFileParser()
        self.robots.parse(text.splitlines())
        self.delay = max(self.delay, self.robots.crawl_delay(AGENT) or 0)
        rate = self.robots.request_rate(AGENT)
        if rate and rate.requests:
            self.delay = max(self.delay, rate.seconds / rate.requests)

    def listing(self, page: int) -> tuple[str, dict]:
        html = self.get(BASE + "?" + urlencode({"active": 1, "PAGEN_1": page}))
        return html, parse_list(html, page)


def schema_sql() -> str:
    columns = ",\n".join(f"    {f} text NOT NULL" for f in TEXT_FIELDS)
    return f"""-- Standalone global registry; PostgreSQL 14+; schema contract version 1.
CREATE SCHEMA IF NOT EXISTS {SCHEMA};
CREATE TABLE IF NOT EXISTS {SCHEMA}.schema_version (version integer PRIMARY KEY CHECK(version=1));
INSERT INTO {SCHEMA}.schema_version VALUES (1) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS {SCHEMA}.crawl_runs (
    id uuid PRIMARY KEY, started_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz, heartbeat_at timestamptz NOT NULL DEFAULT now(),
    state text NOT NULL CHECK(state IN ('running','paused','failed','complete')),
    phase text NOT NULL DEFAULT 'discovery' CHECK(phase IN ('discovery','details','complete')),
    next_page integer NOT NULL DEFAULT 1, expected_total integer,
    first_signature text, parser_version text NOT NULL, note text
);
CREATE TABLE IF NOT EXISTS {SCHEMA}.crawl_pages (
    run_id uuid NOT NULL REFERENCES {SCHEMA}.crawl_runs(id), page_number integer NOT NULL,
    first_record integer NOT NULL, last_record integer NOT NULL, total integer NOT NULL,
    signature text NOT NULL, fetched_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(run_id, page_number)
);
CREATE TABLE IF NOT EXISTS {SCHEMA}.crawl_queue (
    run_id uuid NOT NULL REFERENCES {SCHEMA}.crawl_runs(id), license_number text NOT NULL,
    source_url text NOT NULL, page_number integer NOT NULL, listing_status text,
    state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','done','skipped','error')),
    attempts integer NOT NULL DEFAULT 0, error text, checked_at timestamptz,
    PRIMARY KEY(run_id, license_number)
);
ALTER TABLE {SCHEMA}.crawl_queue ADD COLUMN IF NOT EXISTS listing_status text;
CREATE INDEX IF NOT EXISTS rtn170_queue_state ON {SCHEMA}.crawl_queue(run_id,state,page_number);
CREATE TABLE IF NOT EXISTS {SCHEMA}.licenses (
    id uuid PRIMARY KEY, source_key text NOT NULL DEFAULT '{SOURCE}' CHECK(source_key='{SOURCE}'),
{columns},
    registered_on date, valid_until date, valid_indefinite boolean NOT NULL,
    source_url text NOT NULL, source_hash text NOT NULL,
    last_observed_status text NOT NULL,
    missing_from_latest_scan boolean NOT NULL DEFAULT false,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    last_confirmed_active_at timestamptz NOT NULL DEFAULT now(),
    last_status_check_at timestamptz NOT NULL DEFAULT now(),
    last_changed_at timestamptz NOT NULL DEFAULT now(),
    last_seen_run_id uuid NOT NULL REFERENCES {SCHEMA}.crawl_runs(id),
    parser_version text NOT NULL,
    UNIQUE(source_key, license_number),
    CHECK(source_status='Действующая')
);
CREATE INDEX IF NOT EXISTS rtn170_licensee_inn ON {SCHEMA}.licenses(licensee_inn);
CREATE INDEX IF NOT EXISTS rtn170_object_inn ON {SCHEMA}.licenses(object_inn);
CREATE INDEX IF NOT EXISTS rtn170_valid_until ON {SCHEMA}.licenses(valid_until);
CREATE TABLE IF NOT EXISTS {SCHEMA}.license_work_items (
    license_id uuid NOT NULL REFERENCES {SCHEMA}.licenses(id), ordinal integer NOT NULL,
    value text NOT NULL, PRIMARY KEY(license_id,ordinal)
);
CREATE TABLE IF NOT EXISTS {SCHEMA}.license_activity_addresses (
    license_id uuid NOT NULL REFERENCES {SCHEMA}.licenses(id), ordinal integer NOT NULL,
    value text NOT NULL, PRIMARY KEY(license_id,ordinal)
);
CREATE TABLE IF NOT EXISTS {SCHEMA}.license_fields (
    license_id uuid NOT NULL REFERENCES {SCHEMA}.licenses(id), ordinal integer NOT NULL,
    section text NOT NULL, label text NOT NULL, value_text text NOT NULL,
    value_html text NOT NULL, mapped_column text, PRIMARY KEY(license_id,ordinal)
);
CREATE TABLE IF NOT EXISTS {SCHEMA}.license_revisions (
    id uuid PRIMARY KEY, license_id uuid NOT NULL REFERENCES {SCHEMA}.licenses(id),
    source_hash text NOT NULL, parsed_data jsonb NOT NULL, html_gzip bytea NOT NULL,
    captured_at timestamptz NOT NULL DEFAULT now(), parser_version text NOT NULL,
    UNIQUE(license_id,source_hash)
);
CREATE TABLE IF NOT EXISTS {SCHEMA}.license_observations (
    run_id uuid NOT NULL REFERENCES {SCHEMA}.crawl_runs(id), license_number text NOT NULL,
    observed_status text NOT NULL, source_url text NOT NULL,
    checked_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(run_id,license_number)
);
CREATE OR REPLACE VIEW {SCHEMA}.active_licenses AS
SELECT * FROM {SCHEMA}.licenses
WHERE last_observed_status IN ('Действующая','Возобновлена') AND NOT missing_from_latest_scan;
REVOKE ALL ON SCHEMA {SCHEMA} FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA {SCHEMA} FROM PUBLIC;
"""


class Store:
    def __init__(self, *, acquire_lock=True):
        import psycopg
        from psycopg.rows import dict_row
        dsn = os.environ.get("RTN_DATABASE_URL")
        if not dsn:
            raise ConfigError("Задайте RTN_DATABASE_URL (не передавайте пароль через CLI)")
        self.conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row, connect_timeout=15)
        self.conn.execute("SET statement_timeout = '60s'")
        self.conn.execute("SET lock_timeout = '5s'")
        self.conn.execute("SET TIME ZONE 'UTC'")
        if acquire_lock and not self.conn.execute("SELECT pg_try_advisory_lock(%s) AS ok", (LOCK,)).fetchone()["ok"]:
            self.conn.close()
            raise ConfigError("Другой процесс уже работает с реестром")

    def close(self):
        self.conn.close()

    def execute(self, query, params=()):
        return self.conn.execute(query.replace("{s}", SCHEMA), params)

    def run(self, run_id):
        result = self.execute("SELECT * FROM {s}.crawl_runs WHERE id=%s", (run_id,)).fetchone()
        if result is None:
            raise ConfigError("Запуск не найден")
        return result

    def start(self, resume):
        if resume:
            run_id = uuid.UUID(resume)
            run = self.run(run_id)
            if run["state"] == "complete":
                raise ConfigError("Запуск завершен; для обновления создайте новый")
            if run["parser_version"] != VERSION:
                raise ConfigError("Версия парсера изменилась; начните новый запуск")
            with self.conn.transaction():
                self.execute("UPDATE {s}.crawl_runs SET state='running',note=NULL,heartbeat_at=now() WHERE id=%s", (run_id,))
                self.execute("UPDATE {s}.crawl_queue SET state='pending',error=NULL WHERE run_id=%s AND state='error'", (run_id,))
        else:
            run_id = uuid.uuid4()
            self.execute("INSERT INTO {s}.crawl_runs(id,state,parser_version) VALUES(%s,'running',%s)", (run_id, VERSION))
        return run_id

    def status(self, run_id, state, note=None):
        self.execute("UPDATE {s}.crawl_runs SET state=%s,note=%s,heartbeat_at=now() WHERE id=%s", (state, note, run_id))

    def enqueue_page(self, run_id, page, parsed):
        with self.conn.transaction():
            run = self.run(run_id)
            if run["expected_total"] is not None and parsed["total"] != run["expected_total"]:
                raise ParseError("Размер реестра изменился в ходе обхода; начните новый запуск")
            prev = self.execute("SELECT last_record FROM {s}.crawl_pages WHERE run_id=%s ORDER BY page_number DESC LIMIT 1", (run_id,)).fetchone()
            if parsed["start"] != (prev["last_record"] + 1 if prev else 1):
                raise ParseError("Пропуск/повтор диапазона пагинации")
            seen = self.execute("SELECT 1 FROM {s}.crawl_pages WHERE run_id=%s AND signature=%s", (run_id, parsed["signature"])).fetchone()
            if seen:
                raise ParseError("Пагинация возвращает повторную страницу")
            for row in parsed["rows"]:
                self.execute("""INSERT INTO {s}.crawl_queue(run_id,license_number,source_url,page_number,listing_status)
                    VALUES(%s,%s,%s,%s,%s) ON CONFLICT(run_id,license_number) DO UPDATE SET
                    source_url=excluded.source_url,page_number=excluded.page_number,
                    listing_status=excluded.listing_status""",
                    (run_id, row["number"], row["url"], page, row["status"]))
            self.execute("""INSERT INTO {s}.crawl_pages(run_id,page_number,first_record,last_record,total,signature)
                VALUES(%s,%s,%s,%s,%s,%s)""", (run_id, page, parsed["start"], parsed["end"], parsed["total"], parsed["signature"]))
            self.execute("""UPDATE {s}.crawl_runs SET next_page=%s, expected_total=%s,
                first_signature=COALESCE(first_signature,%s), heartbeat_at=now(), phase=%s WHERE id=%s""",
                (page + 1, parsed["total"], parsed["signature"],
                 "details" if parsed["end"] == parsed["total"] else "discovery", run_id))

    def pending(self, run_id):
        return self.execute("""SELECT * FROM {s}.crawl_queue WHERE run_id=%s AND state='pending'
            ORDER BY page_number,license_number LIMIT 1""", (run_id,)).fetchone()

    def accept(self, run_id, task, parsed, html):
        from psycopg.types.json import Jsonb
        fields = parsed["fields"]
        active = is_active(fields["source_status"])
        # Preserve the exact status exposed by the card.  source_status stays
        # canonical because the table stores active cards only; the raw value
        # is also retained in license_fields and revision JSON.
        status = fields["source_status"]
        with self.conn.transaction():
            self.execute("""INSERT INTO {s}.license_observations(run_id,license_number,observed_status,source_url)
                VALUES(%s,%s,%s,%s) ON CONFLICT(run_id,license_number) DO UPDATE SET
                observed_status=excluded.observed_status,checked_at=now()""",
                (run_id, task["license_number"], status, task["source_url"]))
            if active:
                license_id = uuid.uuid5(NAMESPACE, fields["license_number"])
                source_hash = digest(parsed)
                data = dict(fields, source_status="Действующая", id=license_id,
                            source_url=task["source_url"], source_hash=source_hash,
                            last_observed_status=status, last_seen_run_id=run_id, parser_version=VERSION)
                cols = list(data)
                updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")
                query = "INSERT INTO {s}.licenses(" + ",".join(cols) + ") VALUES(" + ",".join(["%s"] * len(cols)) + ") "
                query += "ON CONFLICT(source_key,license_number) DO UPDATE SET " + updates
                query += ",missing_from_latest_scan=false,last_status_check_at=now(),last_confirmed_active_at=now(),"
                query += "last_changed_at=CASE WHEN {s}.licenses.source_hash<>excluded.source_hash THEN now() ELSE {s}.licenses.last_changed_at END RETURNING id"
                actual_id = self.execute(query, tuple(data.values())).fetchone()["id"]
                # Preserve an existing UUID on conflict (including a previously migrated row).
                for table, items in (("license_work_items", parsed["work_items"]),
                                     ("license_activity_addresses", parsed["address_items"])):
                    self.execute("DELETE FROM {s}." + table + " WHERE license_id=%s", (actual_id,))
                    for i, value in enumerate(items, 1):
                        self.execute("INSERT INTO {s}." + table + "(license_id,ordinal,value) VALUES(%s,%s,%s)", (actual_id, i, value))
                self.execute("DELETE FROM {s}.license_fields WHERE license_id=%s", (actual_id,))
                for row in parsed["raw_fields"]:
                    self.execute("""INSERT INTO {s}.license_fields(license_id,ordinal,section,label,value_text,value_html,mapped_column)
                        VALUES(%s,%s,%s,%s,%s,%s,%s)""", (actual_id, row["ordinal"], row["section"], row["label"], row["value_text"], row["value_html"], row["mapped_column"]))
                revision_id = uuid.uuid5(actual_id, source_hash)
                self.execute("""INSERT INTO {s}.license_revisions(id,license_id,source_hash,parsed_data,html_gzip,parser_version)
                    VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(license_id,source_hash) DO NOTHING""",
                    (revision_id, actual_id, source_hash, Jsonb(parsed), gzip.compress(html.encode("utf-8"), mtime=0), VERSION))
            else:
                # No new inactive card is imported. Only a status observation is retained.
                self.execute("""UPDATE {s}.licenses SET last_observed_status=%s,last_status_check_at=now()
                    WHERE source_key=%s AND license_number=%s""", (status, SOURCE, task["license_number"]))
            self.execute("""UPDATE {s}.crawl_queue SET state=%s,error=NULL,checked_at=now()
                WHERE run_id=%s AND license_number=%s""", ("done" if active else "skipped", run_id, task["license_number"]))
            self.execute("UPDATE {s}.crawl_runs SET heartbeat_at=now() WHERE id=%s", (run_id,))

    def finalize(self, run_id, first_page):
        with self.conn.transaction():
            run = self.run(run_id)
            if run["phase"] != "details":
                raise ParseError("Обход страниц еще не завершен")
            newer = self.execute("""SELECT 1 FROM {s}.crawl_runs WHERE state='complete'
                AND started_at > %s LIMIT 1""", (run["started_at"],)).fetchone()
            if newer:
                raise ParseError("Уже завершен более новый обход; старый запуск нельзя финализировать")
            stats = self.execute("""SELECT count(*) AS n,count(*) FILTER(WHERE state IN ('error','pending')) AS bad
                FROM {s}.crawl_queue WHERE run_id=%s""", (run_id,)).fetchone()
            if stats["bad"] or stats["n"] != run["expected_total"]:
                raise ParseError("Обход неполный: ошибки карточек/дубли/изменение списка; полная синхронизация запрещена")
            if first_page["total"] != run["expected_total"] or first_page["signature"] != run["first_signature"]:
                raise ParseError("Первая страница изменилась; начните новый запуск")
            # Absence is NOT revocation. Keep historical data and an explicit flag.
            self.execute("""UPDATE {s}.licenses l SET missing_from_latest_scan=true
                WHERE NOT EXISTS(SELECT 1 FROM {s}.crawl_queue q WHERE q.run_id=%s AND q.license_number=l.license_number)
                AND l.last_status_check_at <= (SELECT started_at FROM {s}.crawl_runs WHERE id=%s)""", (run_id, run_id))
            self.execute("""UPDATE {s}.crawl_runs SET state='complete',phase='complete',finished_at=now(),heartbeat_at=now(),note=NULL
                WHERE id=%s""", (run_id,))


def crawl(store, args):
    run_id = store.start(args.resume)
    LOG.info("run_id=%s", run_id)
    with contextlib.closing(Client(args.delay)) as client:
        try:
            client.load_robots()
            run = store.run(run_id)
            if run["expected_total"] is not None:
                _, first = client.listing(1)
                if first["total"] != run["expected_total"] or first["signature"] != run["first_signature"]:
                    raise ParseError("Список изменился после паузы; создайте новый запуск")
            pages_done = 0
            while store.run(run_id)["phase"] == "discovery":
                if args.max_pages and pages_done >= args.max_pages:
                    store.status(run_id, "paused", "Лимит страниц данного запуска процесса")
                    return 2
                page = store.run(run_id)["next_page"]
                _, listing = client.listing(page)
                if listing.get("inactive_row_count"):
                    LOG.warning("run=%s page=%s active_filter_returned_inactive=%s; queued for card verification",
                                run_id, page, listing["inactive_row_count"])
                store.enqueue_page(run_id, page, listing)
                pages_done += 1
                LOG.info("run=%s page=%s records=%s/%s", run_id, page, listing["end"], listing["total"])
            cards_done = 0
            while task := store.pending(run_id):
                if args.max_cards and cards_done >= args.max_cards:
                    store.status(run_id, "paused", "Лимит карточек данного запуска процесса")
                    return 2
                attempt = task["attempts"] + 1
                store.execute("""UPDATE {s}.crawl_queue SET attempts=attempts+1 WHERE run_id=%s AND license_number=%s""", (run_id, task["license_number"]))
                try:
                    html = client.get(task["source_url"])
                except FetchError as exc:
                    # A single broken card must not prevent collection of the
                    # remaining registry.  Each process-level attempt already
                    # contains four bounded HTTP retries.  Preserve the queue
                    # row and the exact reason; after four rounds it becomes a
                    # terminal, auditable skipped item accepted by finalize().
                    terminal = attempt >= 4
                    listing_status = task.get("listing_status")
                    if terminal and not listing_status:
                        # Queues created by parser versions before listing_status
                        # was introduced can still be resumed.  Re-read only the
                        # task's listing page before accepting a terminal skip so
                        # every skipped card retains an authoritative observation.
                        try:
                            _, listing = client.listing(task["page_number"])
                            matching = [row for row in listing["rows"]
                                        if row["number"] == task["license_number"]]
                            if len(matching) != 1:
                                raise ParseError(
                                    "Не удалось однозначно восстановить статус карточки из списка; "
                                    "список мог измениться"
                                )
                            listing_status = matching[0]["status"]
                            store.execute("""UPDATE {s}.crawl_queue SET listing_status=%s
                                WHERE run_id=%s AND license_number=%s""",
                                (listing_status, run_id, task["license_number"]))
                        except FetchError as listing_exc:
                            store.execute("""UPDATE {s}.crawl_queue SET state='pending',error=%s,checked_at=now()
                                WHERE run_id=%s AND license_number=%s""",
                                (f"Карточка недоступна; статус списка пока не восстановлен: {listing_exc}",
                                 run_id, task["license_number"]))
                            LOG.warning("license=%s listing_status_retry", task["license_number"])
                            time.sleep(min(60, 15 * attempt))
                            continue
                    store.execute("""UPDATE {s}.crawl_queue SET state=%s,error=%s,checked_at=now()
                        WHERE run_id=%s AND license_number=%s""",
                        ("skipped" if terminal else "pending", str(exc), run_id, task["license_number"]))
                    if terminal:
                        if listing_status:
                            store.execute("""INSERT INTO {s}.license_observations
                                (run_id,license_number,observed_status,source_url)
                                VALUES(%s,%s,%s,%s) ON CONFLICT(run_id,license_number) DO UPDATE SET
                                observed_status=excluded.observed_status,source_url=excluded.source_url,
                                checked_at=now()""",
                                (run_id, task["license_number"], listing_status, task["source_url"]))
                        LOG.error("license=%s unavailable_after_attempts=%s; preserved as skipped",
                                  task["license_number"], attempt)
                    else:
                        LOG.warning("license=%s fetch_retry_round=%s", task["license_number"], attempt)
                        time.sleep(min(60, 15 * attempt))
                    continue
                try:
                    parsed = parse_card(html, task["license_number"])
                    store.accept(run_id, task, parsed, html)
                    for warning in parsed["warnings"]:
                        LOG.warning("license=%s %s", task["license_number"], warning)
                except ParseError as exc:
                    store.execute("""UPDATE {s}.crawl_queue SET state='error',error=%s,checked_at=now()
                        WHERE run_id=%s AND license_number=%s""", (str(exc), run_id, task["license_number"]))
                    LOG.error("license=%s parse_error=%s", task["license_number"], exc)
                    # A changed card layout should not trigger thousands of futile requests.
                    store.status(run_id, "paused", str(exc))
                    return 2
                cards_done += 1
                LOG.info("run=%s processed=%s license=%s", run_id, cards_done, task["license_number"])
            _, first = client.listing(1)
            store.finalize(run_id, first)
            LOG.info("run=%s COMPLETE", run_id)
            return 0
        except (SourceBlocked, FetchError, ParseError) as exc:
            store.status(run_id, "paused", str(exc))
            LOG.error("run=%s PAUSED: %s", run_id, exc)
            return 2
        except (KeyboardInterrupt, SystemExit):
            store.status(run_id, "paused", "Процесс остановлен оператором")
            return 130
        except Exception:
            # Never log DSN or arbitrary exception strings containing credentials.
            with contextlib.suppress(Exception):
                store.status(run_id, "failed", "Ошибка БД/программы; диагностика без раскрытия DSN")
            raise


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("print-schema", help="Print standalone PostgreSQL DDL")
    sub.add_parser("init-db", help="Create schema, no automatic production integration")
    sub.add_parser("status", help="Last 10 runs and queue counters")
    for cmd in ("parse-card", "parse-list"):
        parser = sub.add_parser(cmd, help="Offline parse to JSON, no DB or network")
        parser.add_argument("file", type=Path)
    check = sub.add_parser("check-source", help="Check robots, one active page and one card; no DB writes")
    check.add_argument("--delay", type=float, default=3)
    run = sub.add_parser("crawl")
    run.add_argument("--resume", metavar="RUN_UUID")
    run.add_argument("--delay", type=float, default=3)
    run.add_argument("--max-pages", type=int, default=0, help="Per-process limit; 0=unlimited")
    run.add_argument("--max-cards", type=int, default=0, help="Per-process limit; 0=unlimited")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if hasattr(args, "max_pages") and (args.max_pages < 0 or args.max_cards < 0):
        p.error("Лимиты не могут быть отрицательными")
    if args.command == "print-schema":
        print(schema_sql())
        return 0
    if args.command in ("parse-card", "parse-list"):
        from bs4 import UnicodeDammit
        html = UnicodeDammit(args.file.read_bytes()).unicode_markup
        data = parse_card(html) if args.command == "parse-card" else parse_list(html, require_active_filter=False)
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return 0
    if args.command == "check-source":
        with contextlib.closing(Client(args.delay)) as client:
            client.load_robots()
            _, listing = client.listing(1)
            first = listing["rows"][0]
            card = parse_card(client.get(first["url"]), first["number"])
            if not is_active(card["fields"]["source_status"]):
                raise ParseError("Первая карточка уже не действующая; повторите проверку позднее")
            print(json.dumps({"active_total": listing["total"], "list": listing, "card": card}, ensure_ascii=False, indent=2))
        return 0
    with contextlib.closing(Store(acquire_lock=args.command != "status")) as store:
        if args.command == "init-db":
            with store.conn.transaction():
                store.conn.execute(schema_sql())
            print("Schema rtn170_shared ready (v1)")
            return 0
        versions = store.execute("SELECT version FROM {s}.schema_version").fetchall()
        if [r["version"] for r in versions] != [1]:
            raise ConfigError("Несовместимая версия схемы БД")
        if args.command == "status":
            rows = store.execute("""SELECT r.*,q.state AS queue_state,count(q.license_number) AS queue_count
                FROM {s}.crawl_runs r LEFT JOIN {s}.crawl_queue q ON q.run_id=r.id
                WHERE r.id IN (SELECT id FROM {s}.crawl_runs ORDER BY started_at DESC LIMIT 10)
                GROUP BY r.id,q.state ORDER BY r.started_at DESC,q.state""").fetchall()
            print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
            return 0
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        return crawl(store, args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SourceBlocked, ParseError, FetchError, ConfigError) as exc:
        LOG.error("%s", exc)
        sys.exit(2)
    except Exception as exc:
        LOG.error("Ошибка %s. Проверьте окружение, БД и инструкцию; секреты в лог не выводятся.", type(exc).__name__)
        sys.exit(1)
