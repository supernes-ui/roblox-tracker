"""Wspólny skaner i historia publicznych instancji Roblox."""
import json
import os
from pathlib import Path
import re
import sqlite3
from time import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener, urlopen

SCAN_INTERVAL = 60
PAGE_DELAY = 1
PAGE_LIMIT = 20
DEFAULT_PLACE = "102062722692474"
TUNNEL_PORT = 8765
HISTORY = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "RobloxServerAge" / "history.sqlite3"
TUNNEL_OPENER = build_opener(ProxyHandler({}))


def fetch_page(place, cursor):
    query = {"limit": 100, "sortOrder": "Asc", "excludeFullGames": "false"}
    if cursor:
        query["cursor"] = cursor
    url = f"https://games.roblox.com/v1/games/{place}/servers/Public?{urlencode(query)}"
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "RobloxServerAge/1.0"})
    with urlopen(request, timeout=12) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("Roblox zwrócił nieoczekiwany format listy serwerów.")
    return payload


def scan(place, stop, fetch=fetch_page):
    rows, cursors = {}, set()
    cursor = None
    for _ in range(PAGE_LIMIT):
        if stop.is_set():
            break
        payload = fetch(place, cursor)
        seen_at = int(time())
        for item in payload["data"]:
            if not isinstance(item, dict):
                continue
            job = item.get("id")
            if isinstance(job, str) and job:
                slots = f'{item.get("playing", "?")}/{item.get("maxPlayers", "?")}'
                rows.setdefault(job, (seen_at, slots))
        cursor = payload.get("nextPageCursor")
        if not cursor:
            return rows, False
        if not isinstance(cursor, str) or cursor in cursors:
            return rows, True
        cursors.add(cursor)
        if stop.wait(PAGE_DELAY):
            break
    return rows, True


def remember(db, place, rows):
    db.execute("""
        CREATE TABLE IF NOT EXISTS observed (
            place TEXT NOT NULL, job TEXT NOT NULL, first_seen INTEGER NOT NULL,
            PRIMARY KEY (place, job)
        )
    """)
    with db:
        db.executemany(
            """INSERT INTO observed (place, job, first_seen) VALUES (?, ?, ?)
               ON CONFLICT(place, job) DO UPDATE SET
               first_seen = MIN(observed.first_seen, excluded.first_seen)""",
            ((place, job, stamp) for job, (stamp, _) in rows.items()),
        )
        return [
            (job, db.execute("SELECT first_seen FROM observed WHERE place=? AND job=?", (place, job)).fetchone()[0],
             stamp, slots)
            for job, (stamp, slots) in rows.items()
        ]


def error_message(error):
    if isinstance(error, HTTPError):
        if error.code == 429:
            return "Roblox ogranicza zapytania. Ponowna próba za 2 minuty.", 120
        return f"Roblox zwrócił błąd HTTP {error.code}. Ponowna próba za minutę.", SCAN_INTERVAL
    if isinstance(error, (URLError, TimeoutError)):
        return "Brak połączenia z Roblox. Ponowna próba za minutę.", SCAN_INTERVAL
    return f"Błąd skanowania: {error}", SCAN_INTERVAL


def monitor(place, stop, results, history=HISTORY):
    try:
        history.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(history)
    except (OSError, sqlite3.Error) as error:
        results.put(("fatal", f"Nie można otworzyć historii: {error}"))
        return
    try:
        while not stop.is_set():
            try:
                rows, partial = scan(place, stop)
                if stop.is_set():
                    break
                results.put(("snapshot", (remember(db, place, rows), partial, int(time()))))
                delay = SCAN_INTERVAL
            except (OSError, ValueError, sqlite3.Error) as error:
                message, delay = error_message(error)
                results.put(("error", message))
            stop.wait(delay)
    finally:
        db.close()


def fetch_remote(place):
    request = Request(f"http://127.0.0.1:{TUNNEL_PORT}/snapshot", headers={"Accept": "application/json"})
    with TUNNEL_OPENER.open(request, timeout=5) as response:
        return parse_snapshot(response.read(2_000_001), place, "VPS")


def parse_snapshot(raw, place, source):
    if len(raw) > 2_000_000:
        raise ValueError(f"Odpowiedź źródła {source} jest zbyt duża.")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("place") != place:
        raise ValueError(f"Źródło {source} śledzi inne miejsce Roblox.")
    status = data.get("status")
    if not isinstance(status, str):
        raise ValueError(f"Źródło {source} zwróciło nieprawidłowy status.")
    stamp = data.get("stamp")
    if stamp is None:
        return None, status
    records = data.get("rows")
    partial = data.get("partial")
    if type(stamp) is not int or not 0 < stamp < 4_294_967_296 or not isinstance(records, list) or type(partial) is not bool:
        raise ValueError(f"Źródło {source} zwróciło nieprawidłowe dane skanu.")
    for row in records:
        if (not isinstance(row, list) or len(row) != 4 or not isinstance(row[0], str) or not row[0]
                or type(row[1]) is not int or type(row[2]) is not int or not 0 < row[1] <= row[2] <= stamp
                or not isinstance(row[3], str)):
            raise ValueError(f"Źródło {source} zwróciło nieprawidłowe dane instancji.")
    return (records, partial, stamp), status


def github_repository(value):
    value = value.strip().rstrip("/")
    url = urlsplit(value)
    if url.scheme or url.netloc:
        if url.scheme != "https" or url.netloc.lower() != "github.com":
            raise ValueError("Wklej adres publicznego repozytorium z github.com.")
        value = url.path.strip("/")
    if value.endswith(".git"):
        value = value[:-4]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", value):
        raise ValueError("Wklej adres repozytorium GitHub albo wpisz login/nazwa-repozytorium.")
    if value.split("/")[1] in {".", ".."}:
        raise ValueError("Podaj nazwę repozytorium GitHub.")
    return value


def fetch_github(place, repository):
    url = f"https://raw.githubusercontent.com/{repository}/HEAD/dane_trackera.json"
    request = Request(url, headers={"Accept": "application/json", "Cache-Control": "no-cache"})
    try:
        with urlopen(request, timeout=10) as response:
            snapshot, status = parse_snapshot(response.read(2_000_001), place, "GitHub")
    except HTTPError as error:
        if error.code == 404:
            return None, "Brak wyników GitHub. Sprawdź adres repozytorium i pierwszy skan w zakładce Actions."
        raise
    if snapshot is not None and time() - snapshot[2] > 15 * 60:
        status = "Ostatni skan GitHub ma ponad 15 minut. Sprawdź zakładkę Actions."
    return snapshot, status


def remote_monitor(place, stop, results, history=HISTORY, *, repository=None):
    source = "GitHub" if repository else "VPS"
    connection_error = ("Nie udało się pobrać wyników z GitHub."
                        if repository else "Brak połączenia z VPS. Sprawdź, czy tunel SSH jest otwarty.")
    try:
        history.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(history)
    except (OSError, sqlite3.Error) as error:
        results.put(("fatal", f"Nie można otworzyć historii: {error}"))
        return
    last_stamp, last_status = None, None
    try:
        while not stop.is_set():
            try:
                snapshot, status = fetch_github(place, repository) if repository else fetch_remote(place)
                if snapshot is not None and snapshot[2] != last_stamp:
                    rows, partial, stamp = snapshot
                    first = remember(db, place, {job: (first_seen, slots) for job, first_seen, _, slots in rows})
                    first_by_job = {job: first_seen for job, first_seen, _, _ in first}
                    merged = [(job, first_by_job[job], seen, slots) for job, _, seen, slots in rows]
                    results.put(("snapshot", (merged, partial, stamp)))
                    last_stamp = stamp
                    last_status = None
                if status and status != last_status:
                    results.put(("error", status))
                last_status = status
            except (URLError, TimeoutError):
                status = connection_error
                if status != last_status:
                    results.put(("error", status))
                last_status = status
                last_stamp = None
            except (OSError, ValueError, sqlite3.Error) as error:
                status = f"Błąd odczytu ze źródła {source}: {error}"
                if status != last_status:
                    results.put(("error", status))
                last_status = status
            if stop.wait(60 if repository else 5):
                break
    finally:
        db.close()
