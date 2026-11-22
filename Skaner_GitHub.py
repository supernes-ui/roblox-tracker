"""Wykonuje pojedynczy skan Roblox i zapisuje historię między uruchomieniami Actions."""
import json
from pathlib import Path
from threading import Event
from time import time

from roblox_scan import DEFAULT_PLACE, scan

KEEP_SECONDS = 14 * 24 * 3600


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        file.write("\n")
    temporary.replace(path)


def update_files(directory):
    history_path = directory / "historia_trackera.json"
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.exists() else {}
    observations, partial = scan(DEFAULT_PLACE, Event())
    stamp = int(time())
    history = {job: timestamps for job, timestamps in history.items()
               if stamp - timestamps[1] <= KEEP_SECONDS or job in observations}
    rows = []
    for job, (seen, slots) in sorted(observations.items()):
        previous = history.get(job)
        first = min(previous[0], seen) if previous else seen
        history[job] = [first, seen]
        rows.append([job, first, seen, slots])
    write_json(history_path, history)
    write_json(directory / "dane_trackera.json", {
        "place": DEFAULT_PLACE,
        "stamp": stamp,
        "status": "",
        "partial": partial,
        "rows": rows,
    })
    print(f"Roblox: {len(rows)} instancji, {len(history)} wpisów historii.", flush=True)


if __name__ == "__main__":
    update_files(Path(__file__).resolve().parent)
