"""Offline unit tests + opt-in integration tests on a disposable database only."""
import copy
import os
from argparse import Namespace
from pathlib import Path
import uuid
from unittest.mock import MagicMock, patch

import pytest

import rtn170_parser as p

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def card():
    return (FIX / "card_reconstructed_from_screenshot.html").read_text(encoding="utf-8")


def test_17_fields_and_empty_value(card):
    data = p.parse_card(card, "ЦО-11-101-14442")
    assert len(data["raw_fields"]) == 17
    assert set(p.TEXT_FIELDS) <= data["fields"].keys()
    assert data["fields"]["registered_on"] == "2025-04-29"
    assert data["fields"]["valid_until"] == "2030-04-29"
    assert data["fields"]["licensee_inn"] == "4629003691"
    assert data["fields"]["activity_addresses_text"] == ""
    assert data["address_items"] == []
    assert len(data["work_items"]) == 1
    assert data["warnings"] == []


@pytest.mark.parametrize("status,want", [("Действующая", True), ("Возобновлена", True), ("Недействующая", False),
    ("Приостановлена", False), ("Действует", False), ("", False), (" действующая ", True)])
def test_exact_status(status, want):
    assert p.is_active(status) is want


def test_source_list_fixture():
    html = (FIX / "list_from_supplied_html.html").read_text(encoding="utf-8")
    data = p.parse_list(html, require_active_filter=False)
    assert (data["start"], data["end"], data["total"], data["last_page_link"]) == (1, 10, 32251, 3226)
    assert data["all_row_count"] == 10
    assert len(data["rows"]) == 10  # Source anomalies are queued and verified against the card.
    assert data["inactive_row_count"] > 0
    assert sum(not p.is_active(r["status"]) for r in data["rows"]) == data["inactive_row_count"]
    assert data["rows"][0]["number"] == "ЦО-11-101-14442"
    assert data["rows"][0]["url"].endswith("&ajax=1")
    with pytest.raises(p.ParseError, match="фильтр"):
        p.parse_list(html)


def listing_html(numbers, total=None, start=1):
    from urllib.parse import quote
    total = total or len(numbers)
    return ('<form id="licenses_form"><input name="active" checked></form>'
            '<table id="licenses_list"><tr><th>Номер лицензии</th></tr>' +
            "".join(f'<tr><td><a href="license.php?licNum={quote(n)}">{n}</a></td>'
                    '<td>A</td><td>B</td><td>C</td><td>D</td><td>Действующая</td></tr>' for n in numbers) +
            f'</table><font>{start} - {start + len(numbers) - 1} из {total}</font>')


def test_active_list():
    assert p.parse_list(listing_html(["ЦО-1", "ЦО-2"]))["total"] == 2


def test_counter_with_thousands_spaces():
    html = listing_html(["ЦО-1"], total=32251).replace("32251", "32 251")
    assert p.parse_list(html)["total"] == 32251


def test_missing_fields_fail(card):
    with pytest.raises(p.ParseError, match="Отсутствуют"):
        p.parse_card(card.replace("Срок действия лицензии", "Другая подпись"))


def test_empty_fields_distinct_from_missing(card):
    assert p.parse_card(card.replace("4629003691", ""))["fields"]["licensee_inn"] == ""


def test_unknown_field_preserved(card):
    card = card.replace("</table>", "<tr><th>Новое поле</th><td>Значение</td></tr></table>")
    data = p.parse_card(card)
    assert len(data["raw_fields"]) == 18
    assert data["raw_fields"][-1]["mapped_column"] is None
    assert data["warnings"]


def test_duplicate_known_field_fail(card):
    card = card.replace("</table>", "<tr><th>Статус</th><td>Действующая</td></tr></table>")
    with pytest.raises(p.ParseError, match="Повтор"):
        p.parse_card(card)


def test_date_raw_and_indefinite(card):
    data = p.parse_card(card.replace("29.04.2030", "Бессрочно"))
    assert data["fields"]["valid_until"] is None
    assert data["fields"]["valid_indefinite"] is True
    data = p.parse_card(card.replace("29.04.2030", "31.02.2030"))
    assert data["fields"]["valid_until_raw"] == "31.02.2030"
    assert data["fields"]["valid_until"] is None
    assert data["warnings"]


def test_number_mismatch(card):
    with pytest.raises(p.ParseError, match="не совпадает"):
        p.parse_card(card, "ЦО-НЕВЕРНЫЙ")


def test_multi_items_no_comma_split(card):
    card = card.replace("<td></td>", "<td><ul><li>Москва, ул. А, 1</li><li>Курск, ул. Б, 2</li></ul></td>")
    assert p.parse_card(card)["address_items"] == ["Москва, ул. А, 1", "Курск, ул. Б, 2"]


def test_inactive_filter_anomaly_is_preserved_for_card_verification():
    parsed = p.parse_list(listing_html(["A"]).replace("Действующая", "Недействующая"))
    assert parsed["inactive_row_count"] == 1
    assert parsed["rows"][0]["status"] == "Недействующая"


@pytest.mark.parametrize("url", ["http://www.gosnadzor.ru/", "https://evil.example/license.php?licNum=x", "https://www.gosnadzor.ru/admin/"])
def test_external_urls_rejected(url):
    with pytest.raises(p.SourceBlocked):
        p.detail_url(url)


def test_layout_wrapping_table(card):
    assert p.parse_card("<table><tr><td>" + card + "</td></tr></table>")["fields"]["licensee_inn"] == "4629003691"


def test_http_forbidden_no_retry():
    c = p.Client()
    response = requests_response(403)
    with patch.object(c.session, "get", return_value=response) as get, patch.object(p.time, "sleep"):
        with pytest.raises(p.SourceBlocked):
            c.get(p.BASE)
        assert get.call_count == 1
    c.close()


def requests_response(status):
    from unittest.mock import MagicMock
    r = MagicMock()
    r.__enter__.return_value = r
    r.status_code = status
    r.headers = {}
    return r


@pytest.mark.parametrize(
    "item,want",
    [
        ({"protocol": "Https", "host": "1.2.3.4", "port": 8080}, "http://1.2.3.4:8080"),
        ({"protocol": "Socks5", "host": "1.2.3.4", "port": 1080}, None),
        ({"protocol": "Http", "host": "not-an-ip", "port": 8080}, None),
        ({"protocol": "Http", "host": "1.2.3.4", "port": 70000}, None),
    ],
)
def test_proxyharbor_proxy_validation(item, want):
    assert p.ProxyHarborPool._validated_proxy_url(item) == want


def test_proxyharbor_is_opt_in(monkeypatch):
    monkeypatch.delenv("PROXYHARBOR_API_TOKEN", raising=False)
    monkeypatch.delenv("BEST_PROXIES_API_KEY", raising=False)
    assert p.ProxyHarborPool.from_environment() is None
    assert p.BestProxiesPool.from_environment() is None
    assert p.proxy_pool_from_environment() is None


def test_composite_proxy_pool_falls_back():
    first, second = MagicMock(), MagicMock()
    first.select.side_effect = p.FetchError("none")
    second.select.return_value = "http://1.2.3.4:8080"
    pool = p.CompositeProxyPool([first, second])
    assert pool.select() == "http://1.2.3.4:8080"
    pool.reject("http://1.2.3.4:8080")
    first.reject.assert_called_once()
    second.reject.assert_called_once()


def test_transport_failure_switches_to_verified_proxy(monkeypatch):
    import requests

    monkeypatch.delenv("PROXYHARBOR_API_TOKEN", raising=False)
    c = p.Client()
    pool = MagicMock()
    pool.select.return_value = "http://1.2.3.4:8080"
    c.proxy_pool = pool
    response = requests_response(200)
    response.iter_content.return_value = [b"User-agent: *\nAllow: /\n"]
    with patch.object(c.session, "get", side_effect=[requests.ConnectionError(), response]) as get, \
            patch.object(p.time, "sleep"):
        assert "Allow" in c.get(p.PROXY_PROBE_URL, robots_check=False)
    pool.select.assert_called_once_with()
    assert get.call_args_list[0].kwargs["proxies"] is None
    assert get.call_args_list[1].kwargs["proxies"] == {
        "http": "http://1.2.3.4:8080", "https": "http://1.2.3.4:8080"
    }
    c.close()


@pytest.mark.parametrize("status", [302, 429])
def test_redirect_and_rate_limit_stop(status):
    c = p.Client()
    with patch.object(c.session, "get", return_value=requests_response(status)) as get, patch.object(p.time, "sleep"):
        with pytest.raises(p.SourceBlocked):
            c.get(p.BASE)
        assert get.call_count == 1
    c.close()


def test_bot_page_200_stop():
    c = p.Client()
    r = requests_response(200)
    r.iter_content.return_value = [b"<html>Verify you are human</html>"]
    with patch.object(c.session, "get", return_value=r), patch.object(p.time, "sleep"):
        with pytest.raises(p.SourceBlocked):
            c.get(p.BASE)
    c.close()


def test_empty_200_retried_then_stops():
    c = p.Client()
    r = requests_response(200)
    r.iter_content.return_value = []
    with patch.object(c.session, "get", return_value=r) as get, patch.object(p.time, "sleep"):
        with pytest.raises(p.FetchError, match="после четырех попыток"):
            c.get(p.BASE)
        assert get.call_count == 4
    c.close()


def test_crawl_preserves_terminal_empty_card_and_continues():
    run_id = uuid.uuid4()
    broken = {"license_number": "BROKEN", "source_url": p.BASE + "license.php?licNum=BROKEN&ajax=1",
              "attempts": 3, "listing_status": "Действующая"}
    good = {"license_number": "GOOD", "source_url": p.BASE + "license.php?licNum=GOOD&ajax=1",
            "attempts": 0}
    store = MagicMock()
    store.start.return_value = run_id
    store.run.return_value = {"expected_total": None, "phase": "details"}
    store.pending.side_effect = [broken, good, None]
    client = MagicMock()
    client.get.side_effect = [p.FetchError("Пустое тело ответа после четырех попыток"), "card html"]
    client.listing.return_value = ("first html", {"total": 2, "signature": "sig"})
    parsed = {"warnings": []}
    args = Namespace(resume=str(run_id), delay=3, max_pages=0, max_cards=0)

    with patch.object(p, "Client", return_value=client), patch.object(p, "parse_card", return_value=parsed):
        assert p.crawl(store, args) == 0

    terminal_updates = [call for call in store.execute.call_args_list
                        if len(call.args) > 1 and call.args[1] and call.args[1][0] == "skipped"]
    assert len(terminal_updates) == 1
    assert any("license_observations" in call.args[0] for call in store.execute.call_args_list)
    store.accept.assert_called_once_with(run_id, good, parsed, "card html")
    store.finalize.assert_called_once()


def test_crawl_recovers_listing_status_for_legacy_queue_before_skip():
    run_id = uuid.uuid4()
    broken = {"license_number": "BROKEN", "source_url": p.BASE + "license.php?licNum=BROKEN&ajax=1",
              "page_number": 7, "attempts": 3, "listing_status": None}
    store = MagicMock()
    store.start.return_value = run_id
    store.run.return_value = {"expected_total": None, "phase": "details"}
    store.pending.side_effect = [broken, None]
    client = MagicMock()
    client.get.side_effect = p.FetchError("Пустое тело ответа после четырех попыток")
    listing = {"total": 1, "signature": "sig",
               "rows": [{"number": "BROKEN", "status": "Действующая"}]}
    client.listing.return_value = ("listing html", listing)
    args = Namespace(resume=str(run_id), delay=3, max_pages=0, max_cards=0)

    with patch.object(p, "Client", return_value=client):
        assert p.crawl(store, args) == 0

    assert any("SET listing_status=%s" in call.args[0]
               and call.args[1][0] == "Действующая"
               for call in store.execute.call_args_list)
    assert any("license_observations" in call.args[0]
               and call.args[1][2] == "Действующая"
               for call in store.execute.call_args_list)
    client.listing.assert_any_call(7)


@pytest.fixture
def db(monkeypatch):
    dsn = os.environ.get("RTN_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("No RTN_TEST_DATABASE_URL (disposable DB required)")
    # Name MUST include rtn170_test; production URLs are rejected before connecting.
    from psycopg.conninfo import conninfo_to_dict
    if "rtn170_test" not in conninfo_to_dict(dsn).get("dbname", ""):
        pytest.fail("Only a disposable DB named *rtn170_test* is allowed")
    monkeypatch.setenv("RTN_DATABASE_URL", dsn)
    store = p.Store()
    store.conn.execute(p.schema_sql())
    yield store
    store.close()


def task_for(num, run):
    from urllib.parse import quote
    return {"license_number": num, "source_url": p.detail_url("license.php?licNum=" + quote(num)), "run_id": run}


def test_database_upsert_history_and_inactive(db, card):
    number = "TEST-" + str(uuid.uuid4())
    card = card.replace("ЦО-11-101-14442", number)
    number = p.number_key(number)
    run = db.start(None)
    listing = p.parse_list(listing_html([number]))
    db.enqueue_page(run, 1, listing)
    task = task_for(number, run)
    db.accept(run, task, p.parse_card(card), card)
    db.accept(run, task, p.parse_card(card), card)
    row = db.execute("SELECT * FROM {s}.licenses WHERE license_number=%s", (number,)).fetchone()
    lid = row["id"]
    assert db.execute("SELECT count(*) AS n FROM {s}.license_fields WHERE license_id=%s", (lid,)).fetchone()["n"] == 17
    assert db.execute("SELECT count(*) AS n FROM {s}.license_revisions WHERE license_id=%s", (lid,)).fetchone()["n"] == 1
    changed = card.replace("29.04.2030", "29.04.2031")
    db.accept(run, task, p.parse_card(changed), changed)
    assert db.execute("SELECT count(*) AS n FROM {s}.license_revisions WHERE license_id=%s", (lid,)).fetchone()["n"] == 2
    inactive = changed.replace("Действующая", "Недействующая")
    db.accept(run, task, p.parse_card(inactive), inactive)
    assert not db.execute("SELECT 1 FROM {s}.active_licenses WHERE id=%s", (lid,)).fetchone()
    assert db.execute("SELECT source_status FROM {s}.licenses WHERE id=%s", (lid,)).fetchone()["source_status"] == "Действующая"
    db.finalize(run, listing)
    assert db.run(run)["state"] == "complete"


def test_database_inactive_never_imported(db, card):
    number = "TEST-" + str(uuid.uuid4()).upper()
    card = card.replace("ЦО-11-101-14442", number).replace("Действующая", "Недействующая")
    run = db.start(None)
    db.enqueue_page(run, 1, p.parse_list(listing_html([number])))
    db.accept(run, task_for(number, run), p.parse_card(card), card)
    assert not db.execute("SELECT 1 FROM {s}.licenses WHERE license_number=%s", (number,)).fetchone()


def test_database_resumed_status_is_active_and_preserved(db, card):
    number = "TEST-" + str(uuid.uuid4()).upper()
    resumed = card.replace("ЦО-11-101-14442", number).replace("Действующая", "Возобновлена")
    run = db.start(None)
    db.enqueue_page(run, 1, p.parse_list(listing_html([number])))
    db.accept(run, task_for(number, run), p.parse_card(resumed), resumed)
    row = db.execute("SELECT source_status,last_observed_status FROM {s}.licenses WHERE license_number=%s", (number,)).fetchone()
    assert row["source_status"] == "Действующая"
    assert row["last_observed_status"] == "Возобновлена"
    assert db.execute("SELECT 1 FROM {s}.active_licenses WHERE license_number=%s", (number,)).fetchone()


def test_database_incomplete_cannot_finalize(db):
    run = db.start(None)
    page = p.parse_list(listing_html(["TEST-" + str(uuid.uuid4())]))
    db.enqueue_page(run, 1, page)
    with pytest.raises(p.ParseError, match="неполный"):
        db.finalize(run, page)
    assert db.run(run)["state"] != "complete"


def test_database_resume_and_pagination(db):
    run = db.start(None)
    n1, n2 = "TEST-" + str(uuid.uuid4()), "TEST-" + str(uuid.uuid4())
    first = p.parse_list(listing_html([n1], total=2))
    db.enqueue_page(run, 1, first)
    db.status(run, "paused")
    db.start(str(run))
    assert db.run(run)["next_page"] == 2
    with pytest.raises(p.ParseError):
        db.enqueue_page(run, 2, first)
    db.enqueue_page(run, 2, p.parse_list(listing_html([n2], total=2, start=2), page=2))
    assert db.run(run)["phase"] == "details"


def test_database_transaction_rollback(db, card):
    number = "TEST-" + str(uuid.uuid4()).upper()
    card = card.replace("ЦО-11-101-14442", number)
    run = db.start(None)
    db.enqueue_page(run, 1, p.parse_list(listing_html([number])))
    broken = p.parse_card(card)
    broken["raw_fields"][0]["ordinal"] = None  # Force DB constraint failure.
    with pytest.raises(Exception):
        db.accept(run, task_for(number, run), broken, card)
    assert not db.execute("SELECT 1 FROM {s}.licenses WHERE license_number=%s", (number,)).fetchone()
    assert db.pending(run)["state"] == "pending"
