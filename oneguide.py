"""OneGuide: one XMLTV guide from Gracenote and an HDHomeRun tuner.

Replaces zap2xml + HDHomeRunEPG_To_XmlTv + remove_url. Channels come from the
tuner's lineup. Each one gets its Gracenote listings when Gracenote carries the
same station, and the tuner's guide fills whatever Gracenote leaves out.
"""

import argparse
import gzip
import http.cookiejar
import json
import os
import re
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

VERSION = "1.0"
GRACENOTE = "https://tvlistings.gracenote.com/"
# tmsimg.com, the host zap2xml uses, fails certificate checks (zap2xml turns
# them off). Kodi checks them, so it can't show those images. zpmc serves the
# same files with a valid certificate.
ASSETS = "https://zpmc.tmsimg.com/assets/"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36")
GRID_HOURS = 3
DETAILS_MAX_AGE = 7 * 86400
PLACEHOLDERS = re.compile(r"^(Channel No Longer Available|To Be Announced|TBA)$", re.I)

quiet = False


def log(*args):
    if not quiet:
        print(*args, flush=True)


# ---------------------------------------------------------------- http

class Http:
    def __init__(self, retries, pause):
        self.retries = retries
        self.pause = pause
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.requests = 0
        self.bytes = 0

    def fetch(self, url, data=None, headers=None, ok_missing=False):
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        hdrs = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
        hdrs.update(headers or {})
        for attempt in range(1, self.retries + 1):
            try:
                if self.pause:
                    time.sleep(self.pause)
                self.requests += 1
                with self.opener.open(urllib.request.Request(url, body, hdrs), timeout=60) as r:
                    raw = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    self.bytes += len(raw)
                    return raw.decode("utf-8")
            except urllib.error.HTTPError as e:
                if ok_missing and e.code in (400, 404):
                    return None
                if e.code == 429:
                    # Too many requests: wait as long as the server asks, or a while.
                    wait = int(e.headers.get("Retry-After") or 0) or 30 * attempt
                    log(f"  server busy, waiting {wait} s")
                    time.sleep(wait)
                    continue
                err = f"HTTP {e.code}"
            except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
                err = str(getattr(e, "reason", e))
            log(f"  attempt {attempt} failed: {err}")
            time.sleep(2 + attempt)
        return None

    def json(self, url, **kw):
        text = self.fetch(url, **kw)
        return json.loads(text) if text else None


def read_gz(path):
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return f.read()


def write_gz(path, text):
    tmp = path + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


# ---------------------------------------------------------------- tuner

def broadcast_discover():
    """HDHomeRun discovery on UDP 65001: ask for any tuner, any ID."""
    payload = bytes([0x01, 4, 0, 0, 0, 1, 0x02, 4, 0xFF, 0xFF, 0xFF, 0xFF])
    packet = struct.pack(">HH", 2, len(payload)) + payload
    packet += struct.pack("<I", zlib.crc32(packet) & 0xFFFFFFFF)
    # A plain broadcast leaves on one adapter only, often a VPN or WSL one, so
    # send it from every local address.
    try:
        local = sorted({a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)})
    except OSError:
        local = []
    for _ in range(2):
        for addr in local or ["0.0.0.0"]:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.settimeout(0.5)
                try:
                    s.bind((addr, 0))
                    s.sendto(packet, ("255.255.255.255", 65001))
                    data, (ip, _) = s.recvfrom(2048)
                except OSError:
                    continue
                if len(data) > 4 and struct.unpack(">H", data[:2])[0] == 3:
                    return ip
    return None


def discover_tuner(net, address):
    if address:
        return f"http://{address}"
    # Broadcast first; SiliconDust's cloud list is the fallback for networks
    # where broadcasts don't reach the tuner.
    ip = broadcast_discover()
    if ip:
        return "http://" + ip
    for dev in net.json("https://api.hdhomerun.com/discover") or []:
        if dev.get("DeviceID") and dev.get("LocalIP"):
            return "http://" + dev["LocalIP"]
    sys.exit("No HDHomeRun tuner found. Give its address with --tuner.")


def station_id(url):
    m = re.search(r"/US(\d+)\.", url or "")
    return m.group(1) if m else None


def number_key(num):
    return [int(p) if p.isdigit() else 0 for p in re.split(r"[.\-]", num)]


def read_tuner(net, base, days):
    disc = net.json(base + "/discover.json")
    if not disc:
        sys.exit(f"Can't reach the tuner at {base}.")
    auth = disc.get("DeviceAuth", "")
    lineup = net.json(base + "/lineup.json") or []
    channels = {}
    for item in lineup:
        num = item.get("GuideNumber")
        if num:
            channels[num] = {"number": num, "name": item.get("GuideName", ""),
                             "icon": None, "station": None, "events": []}

    log(f"Tuner {disc.get('ModelNumber', '')} at {base}: {len(channels)} channels")
    if not auth:
        log("  the tuner gave no DeviceAuth, so there is no tuner guide")
        return channels

    start = int(time.time())
    seen = set()
    for day in range(days):
        url = ("https://api.hdhomerun.com/api/guide.php?DeviceAuth="
               + urllib.parse.quote(auth, safe="") + f"&Start={start + day * 86400}&Duration=24")
        data = net.json(url, ok_missing=True)
        if not data:
            break
        count = 0
        for ch in data:
            num = ch.get("GuideNumber")
            c = channels.get(num)
            if c is None:
                continue
            if ch.get("ImageURL"):
                c["icon"] = ch["ImageURL"]
                c["station"] = station_id(ch["ImageURL"])
            for g in ch.get("Guide", []):
                key = (num, g.get("StartTime"))
                if key in seen or not g.get("Title") or g.get("EndTime", 0) <= g.get("StartTime", 0):
                    continue
                seen.add(key)
                c["events"].append(tuner_event(g))
                count += 1
        log(f"  tuner guide day {day + 1}: {count} programs")
    return channels


def tuner_event(g):
    e = {"start": g["StartTime"], "stop": g["EndTime"], "title": g["Title"],
         "subtitle": g.get("EpisodeTitle"), "desc": g.get("Synopsis"),
         "icon": g.get("ImageURL"), "categories": list(g.get("Filter", [])),
         "new": bool(g.get("First")), "live": False, "source": "tuner"}
    m = re.match(r"S(\d+)E(\d+)", g.get("EpisodeNumber", ""))
    if m:
        e["season"], e["episode"] = int(m.group(1)), int(m.group(2))
    if g.get("OriginalAirdate"):
        e["aired"] = g["OriginalAirdate"]
        if g["OriginalAirdate"] + 48 * 3600 > g["StartTime"]:
            e["new"] = True
    return e


# ---------------------------------------------------------------- gracenote

class Gracenote:
    def __init__(self, net, user, password, cache):
        self.net = net
        self.cache = cache
        r = net.json(GRACENOTE + "api/user/login",
                     data={"emailid": user, "password": password,
                           "usertype": "0", "facebookuser": "false"})
        if not r or not r.get("token"):
            sys.exit("Gracenote login failed. Check the user name and password.")
        self.token = r["token"]
        pref = ",".join(f for f, k in (("m", "isMusic"), ("p", "isPPV"), ("h", "isHD")) if r.get(k))
        self.pref = pref or "-"
        props = r.get("properties", {})
        self.postal = props.get("2002", "")
        self.country = props.get("2003", "USA")
        self.headend, _, self.device = props.get("2004", "").partition(":")
        self.device = self.device or "-"
        favs = net.json(GRACENOTE + "api/user/favorites", data={"token": self.token},
                        headers={"X-Requested-With": "XMLHttpRequest"})
        self.favorites = set((favs or {}).get("channels") or [])
        log(f"Gracenote: logged in, {len(self.favorites)} favorite channels")

    def params(self):
        return {"token": self.token, "postalCode": self.postal, "countryCode": self.country,
                "headendId": self.headend, "device": self.device, "aid": "tribnyc2dl"}

    def grid(self, days, use_favorites):
        now = datetime.now().astimezone()
        block = now.replace(hour=now.hour // GRID_HOURS * GRID_HOURS, minute=0, second=0, microsecond=0)
        t = int(block.timestamp())
        stations = {}
        for n in range(days * 24 // GRID_HOURS):
            q = {"time": t, "timespan": GRID_HOURS, "pref": self.pref,
                 "lineupId": f"{self.country}-{self.headend}-DEFAULT", "postalCode": self.postal,
                 "country": self.country, "headendId": self.headend, "device": self.device,
                 "aid": "tribnyc2dl", "token": self.token, "TMSID": "",
                 "AffiliateID": "tribnyc2dl", "FromPage": "TV Grid", "ActivityID": "1",
                 "OVDID": "", "isOverride": "true"}
            path = os.path.join(self.cache, f"grid-{t}.json.gz")
            text = self.net.fetch(GRACENOTE + "api/grid?" + urllib.parse.urlencode(q))
            if text:
                write_gz(path, text)
            elif os.path.exists(path):
                log("  using the last copy of this block")
                text = read_gz(path)
            if not text:
                log(f"  Gracenote has nothing from {datetime.fromtimestamp(t)}, stopping there")
                break
            self.parse_grid(json.loads(text), stations, use_favorites)
            log(f"  Gracenote block {n + 1}/{days * 24 // GRID_HOURS}")
            t += GRID_HOURS * 3600
        return stations

    def parse_grid(self, data, stations, use_favorites):
        for ch in data.get("channels", []):
            sid = ch.get("channelId")
            if not sid or (use_favorites and self.favorites and sid not in self.favorites):
                continue
            num = (ch.get("channelNo") or "").lstrip("0")
            s = stations.setdefault(sid, {"station": sid, "number": num,
                                          "callsign": ch.get("callSign", ""), "icon": None,
                                          "events": {}})
            thumb = ch.get("thumbnail") or ""
            if thumb:
                thumb = thumb.split("?")[0]
                s["icon"] = thumb if thumb.startswith("http") else "https:" + thumb
            for ev in ch.get("events", []):
                e = grid_event(ev)
                if e:
                    s["events"][e["start"]] = e

    def details(self, series_id):
        path = os.path.join(self.cache, f"series-{series_id}.json.gz")
        if os.path.exists(path) and time.time() - os.path.getmtime(path) < DETAILS_MAX_AGE:
            return json.loads(read_gz(path))
        q = self.params()
        q["programSeriesID"] = series_id
        q["clickstream[FromPage]"] = "TV Grid"
        text = self.net.fetch(GRACENOTE + "api/program/overviewDetails", data=q,
                              headers={"X-Requested-With": "XMLHttpRequest"})
        if not text:
            return json.loads(read_gz(path)) if os.path.exists(path) else None
        write_gz(path, text)
        return json.loads(text)


def parse_utc(text):
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%MZ"):
        try:
            return int(datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp())
        except (ValueError, TypeError):
            pass
    return None


image_width = 720


def asset(name):
    # Gracenote's originals run to several MB; its server scales them on request.
    url = ASSETS + name + ".jpg"
    return f"{url}?w={image_width}" if image_width else url


def grid_event(ev):
    p = ev.get("program", {})
    start, stop = parse_utc(ev.get("startTime")), parse_utc(ev.get("endTime"))
    if not start or not stop or stop <= start or not p.get("title"):
        return None
    flags = ev.get("flag") or []
    e = {"start": start, "stop": stop, "title": p["title"], "subtitle": p.get("episodeTitle"),
         "desc": p.get("shortDesc"), "id": p.get("id", ""), "series": p.get("seriesId"),
         "generic": p.get("isGeneric") in (True, "1", 1),
         "year": p.get("releaseYear"), "rating": ev.get("rating"),
         "categories": [re.sub("^filter-", "", g, flags=re.I) for g in ev.get("filter") or []],
         "new": "New" in flags, "live": "Live" in flags,
         "premiere": "Premiere" in flags, "finale": "Finale" in flags,
         "cc": "CC" in (ev.get("tags") or []), "source": "gracenote"}
    if ev.get("thumbnail"):
        e["icon"] = asset(ev["thumbnail"])
    if str(p.get("season") or "").isdigit():
        e["season"] = int(p["season"])
    if str(p.get("episode") or "").isdigit():
        e["episode"] = int(p["episode"])
    if (e["new"] or e["live"]) and e["id"][10:14] != "0000":
        # First airing is today, as a local date; real air dates are UTC midnights.
        e["aired"] = int(datetime.fromtimestamp(start).replace(tzinfo=timezone.utc)
                         .replace(hour=0, minute=0, second=0).timestamp())
    return e


def add_details(e, d):
    genres = [g for g in (d.get("seriesGenres") or "").split("|") if g]
    for g in genres:
        if g.lower() not in (c.lower() for c in e["categories"]):
            e["categories"].append(g)
    tab = d.get("overviewTab") or {}
    e["presenters"] = [c["name"] for c in tab.get("cast") or [] if (c.get("role") or "").lower() == "host"]
    e["actors"] = [(c["name"], c.get("characterName")) for c in tab.get("cast") or []
                   if (c.get("role") or "").lower() != "host"]
    for role in ("director", "writer", "producer"):
        e[role + "s"] = [c["name"] for c in tab.get("crew") or [] if role in (c.get("role") or "").lower()]
    if not e.get("icon") and d.get("seriesImage"):
        e["icon"] = asset(d["seriesImage"])
    if e["id"][:2] in ("MV", "SH") and len(d.get("seriesDescription") or "") > len(e.get("desc") or ""):
        e["desc"] = d["seriesDescription"]
    if e["id"].startswith("EP"):
        eps = [tab.get("upcomingEpisode") or {}] + list(d.get("upcomingEpisodeTab") or [])
        for ue in eps:
            if (ue.get("tmsID") or "").lower() == e["id"].lower():
                aired = parse_utc(ue.get("originalAirDate"))
                if aired and ue["originalAirDate"] != "1000-01-01T00:00Z":
                    e["aired"] = aired
                break


# ---------------------------------------------------------------- merge

def merge(channels, stations, use_tuner, use_gracenote):
    by_station = {s["station"]: s for s in stations.values()}
    by_number = {}
    for s in stations.values():
        by_number.setdefault(s["number"], []).append(s)

    def usable(s):
        return s and any(not PLACEHOLDERS.match(e["title"]) for e in s["events"].values())

    stats = {"gracenote": 0, "tuner": 0, "both": 0}
    for c in channels.values():
        # Same channel number first. The tuner's station IDs come from its logo
        # URLs, which are sometimes a network's logo shared by several stations,
        # so they're only a fallback, mainly for ATSC 3.0 simulcasts (103.1 = 3.1).
        same = [s for s in by_number.get(c["number"], []) if usable(s)]
        g = same[0] if len(same) == 1 else None
        if not g and c["station"] and usable(by_station.get(c["station"])):
            g = by_station[c["station"]]
        if c["number"] in use_tuner:
            g = None
        elif g and c["number"] not in use_gracenote and not agrees(g, c["events"]):
            log(f"  {c['number']} {c['name']}: Gracenote's {g['number']} {g['callsign']} "
                "doesn't match the tuner's listings, using the tuner's (--use-gracenote to change)")
            g = None
        c["gracenote"] = g
        events = []
        if g:
            events = [e for e in g["events"].values() if not PLACEHOLDERS.match(e["title"])]
            c["callsign"] = g["callsign"]
            c["icon"] = c["icon"] or g["icon"]
        covered = sorted((e["start"], e["stop"]) for e in events)
        added = 0
        for e in c["events"]:
            if not overlaps(covered, e["start"], e["stop"]):
                events.append(e)
                added += 1
        if g and added:
            stats["both"] += 1
        elif g:
            stats["gracenote"] += 1
        elif events:
            stats["tuner"] += 1
        c["events"] = sorted(events, key=lambda e: e["start"])
    return stats


def simple(title):
    return re.sub(r"[^a-z0-9]", "", title.lower())


def agrees(station, tuner_events):
    """True unless the tuner's listings plainly disagree with Gracenote's."""
    titles = {}
    for e in station["events"].values():
        titles.setdefault(e["start"] // 60, set()).add(simple(e["title"]))
    checked = matched = 0
    for e in tuner_events:
        minute = e["start"] // 60
        if minute in titles:
            checked += 1
            matched += simple(e["title"]) in titles[minute]
    return checked < 6 or matched * 3 >= checked


def overlaps(spans, start, stop):
    for s, t in spans:
        if s >= stop:
            break
        if t > start:
            return True
    return False


# ---------------------------------------------------------------- images

class Images:
    def __init__(self, net, folder):
        self.net = net
        self.folder = folder
        self.done = {}
        self.fetched = 0
        self.failed = 0
        if folder:
            os.makedirs(folder, exist_ok=True)

    def fetch_all(self, channels):
        """Download every image the guide uses, several at a time."""
        if not self.folder:
            return
        urls = set()
        for c in channels.values():
            urls.add(c["icon"])
            urls.update(e.get("icon") for e in c["events"])
        urls.discard(None)
        urls.discard("")
        with ThreadPoolExecutor(8) as pool:
            for url, path in zip(urls, pool.map(self.download, urls)):
                self.done[url] = path
        self.failed = sum(1 for u, p in self.done.items() if p == u)
        # Drop images the guide hasn't used for two weeks.
        used = {os.path.basename(p) for p in self.done.values()}
        for name in os.listdir(self.folder):
            path = os.path.join(self.folder, name)
            if name not in used and os.path.getmtime(path) < time.time() - 14 * 86400:
                os.remove(path)
            elif name in used:
                os.utime(path)

    def download(self, url):
        parts = urllib.parse.urlparse(url)
        stem, ext = os.path.splitext(parts.path.strip("/").replace("/", "_"))
        size = re.search(r"w=(\d+)", parts.query)
        name = re.sub(r"[^\w.\-]", "_", stem + (f"_w{size.group(1)}" if size else "") + ext)
        path = os.path.abspath(os.path.join(self.folder, name))
        if os.path.exists(path):
            return path
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=30) as r:
                    data = r.read()
                with open(path + ".tmp", "wb") as f:
                    f.write(data)
                os.replace(path + ".tmp", path)
                self.fetched += 1
                return path
            except urllib.error.HTTPError as e:
                if e.code in (403, 404):
                    break
            except Exception:
                pass
            time.sleep(2 + attempt * 3)
        return url  # leave the web address in the guide

    def get(self, url):
        if not url or not self.folder:
            return url
        return self.done.get(url, url)


# ---------------------------------------------------------------- xmltv

def utc_day(t, fmt):
    # fromtimestamp() rejects dates before 1970 on Windows; old reruns have them.
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=t)).strftime(fmt)


def xml_time(t):
    return datetime.fromtimestamp(t).astimezone().strftime("%Y%m%d%H%M%S %z")


def sub(parent, tag, text=None, **attrs):
    el = ET.SubElement(parent, tag, {k.replace("_", "-"): str(v) for k, v in attrs.items()})
    if text is not None:
        el.text = str(text)
    return el


def write_xmltv(path, channels, images, mark):
    tv = ET.Element("tv", {"source-info-name": "Gracenote and HDHomeRun",
                           "generator-info-name": f"OneGuide {VERSION}"})
    ordered = sorted(channels.values(), key=lambda c: number_key(c["number"]))
    for c in ordered:
        ch = sub(tv, "channel", id=c["number"])
        names = [f"{c['number']} {c['name']}", c["number"], c["name"]]
        if c.get("callsign") and c["callsign"] != c["name"]:
            names.append(c["callsign"])
        for n in dict.fromkeys(n for n in names if n.strip()):
            sub(ch, "display-name", n)
        if c["icon"]:
            sub(ch, "icon", src=images.get(c["icon"]))

    total = 0
    for c in ordered:
        for e in c["events"]:
            total += 1
            p = sub(tv, "programme", start=xml_time(e["start"]), stop=xml_time(e["stop"]),
                    channel=c["number"])
            title = e["title"]
            if ("new" in mark and e["new"]) or ("live" in mark and e["live"]):
                title += " *"
            sub(p, "title", title, lang="en")
            if e.get("subtitle"):
                sub(p, "sub-title", e["subtitle"], lang="en")
            if e.get("desc"):
                sub(p, "desc", e["desc"], lang="en")
            credits = [(r, n) for r in ("director", "actor", "writer", "producer", "presenter")
                       for n in e.get(r + "s", [])]
            if credits:
                cr = sub(p, "credits")
                for role, name in credits:
                    if role == "actor":
                        sub(cr, "actor", name[0], **({"role": name[1]} if name[1] else {}))
                    else:
                        sub(cr, role, name)
            if e.get("year"):
                sub(p, "date", e["year"])
            elif e.get("aired") and (e.get("id", "")[:2] == "EP" or e["source"] == "tuner"):
                sub(p, "date", utc_day(e["aired"], "%Y%m%d"))
            for g in dict.fromkeys(e["categories"]):
                sub(p, "category", g[:1].upper() + g[1:], lang="en")
            sub(p, "length", (e["stop"] - e["start"]) // 60, units="minutes")
            if e.get("icon"):
                sub(p, "icon", src=images.get(e["icon"]))
            s, n = e.get("season"), e.get("episode")
            if s or n:
                sub(p, "episode-num", f"S{s or 0:02d}E{n or 0:02d}", system="onscreen")
            m = re.match(r"^(..\d{8})(\d{4})", e.get("id", ""))
            if m:
                sub(p, "episode-num", f"{m.group(1)}.{m.group(2)}", system="dd_progid")
            if s and n:
                sub(p, "episode-num", f"{s - 1}.{n - 1}.", system="xmltv_ns")
            if not e["new"] and not e["live"] and (e.get("id", "")[:2] in ("EP", "SH") or e["source"] == "tuner"):
                attrs = {}
                if e.get("aired"):
                    attrs["start"] = utc_day(e["aired"], "%Y%m%d000000")
                sub(p, "previously-shown", **attrs)
            if e.get("premiere"):
                sub(p, "premiere", "Premiere")
            if e.get("finale"):
                sub(p, "last-chance", "Finale")
            if e["new"]:
                sub(p, "new")
            if e.get("cc"):
                sub(p, "subtitles", type="teletext")
            if e.get("rating"):
                sub(sub(p, "rating"), "value", e["rating"])

    ET.indent(tv, "\t")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE tv SYSTEM "xmltv.dtd">\n')
        f.write(ET.tostring(tv, encoding="utf-8"))
        f.write(b"\n")
    os.replace(tmp, path)
    return len(ordered), total


# ---------------------------------------------------------------- main

def main():
    global quiet, image_width
    ap =argparse.ArgumentParser(description="One XMLTV guide from Gracenote and an HDHomeRun tuner.")
    ap.add_argument("-u", "--user", help="Gracenote (tvlistings.gracenote.com) user name")
    ap.add_argument("-p", "--password", help="Gracenote password")
    ap.add_argument("-d", "--days", type=int, default=5, help="days of Gracenote listings (default 5)")
    ap.add_argument("--tuner-days", type=int, default=3,
                    help="days to ask the tuner's guide for; it stops at what SiliconDust has (default 3)")
    ap.add_argument("-t", "--tuner", help="tuner IP address (default: find it automatically)")
    ap.add_argument("-o", "--output", default="xmltv.xml", help="output file (default xmltv.xml)")
    ap.add_argument("-c", "--cache", default="cache", help="cache folder (default cache)")
    ap.add_argument("-D", "--details", action="store_true",
                    help="add cast, crew, genres and series images (one request per series, cached a week)")
    ap.add_argument("-I", "--image-dir",
                    help="download all program and channel images to this folder and point the guide at them")
    ap.add_argument("-W", "--image-width", type=int, default=720,
                    help="width to ask Gracenote for program images, 0 for the originals (default 720)")
    ap.add_argument("-A", "--mark", default="", help='append " *" to titles: "new", "live" or "new live"')
    ap.add_argument("-a", "--all", action="store_true", help="use all Gracenote channels, not only favorites")
    ap.add_argument("--no-gracenote", action="store_true", help="use only the tuner's guide")
    ap.add_argument("--use-tuner", default="", metavar="LIST",
                    help="channels that always use the tuner's guide, such as 35.3,44.3")
    ap.add_argument("--use-gracenote", default="", metavar="LIST",
                    help="channels that use Gracenote even when its listings disagree with the tuner's")
    ap.add_argument("-S", "--sleep", type=float, default=0, help="seconds to wait between requests")
    ap.add_argument("-r", "--retries", type=int, default=3, help="tries per request (default 3)")
    ap.add_argument("-q", "--quiet", action="store_true", help="no progress output")
    ap.add_argument("--version", action="version", version=f"OneGuide {VERSION}")
    args = ap.parse_args()
    quiet = args.quiet
    image_width = max(0, args.image_width)

    began = time.time()
    os.makedirs(args.cache, exist_ok=True)
    net = Http(max(1, min(args.retries, 20)), args.sleep)

    channels = read_tuner(net, discover_tuner(net, args.tuner), args.tuner_days)

    stations = {}
    if not args.no_gracenote:
        if not args.user or not args.password:
            sys.exit("Give the Gracenote user name and password with -u and -p, or use --no-gracenote.")
        gn = Gracenote(net, args.user, args.password, args.cache)
        stations = gn.grid(args.days, not args.all)
        if args.details:
            needed = {}
            for s in stations.values():
                for e in s["events"].values():
                    if e.get("series") and not e["generic"]:
                        needed.setdefault(e["series"], []).append(e)
            log(f"Gracenote details for {len(needed)} series")
            for i, (sid, evs) in enumerate(needed.items(), 1):
                d = gn.details(sid)
                if d:
                    for e in evs:
                        add_details(e, d)
                if i % 100 == 0:
                    log(f"  {i}/{len(needed)}")

    def channel_list(text):
        return {n.strip() for n in text.split(",") if n.strip()}

    stats = merge(channels, stations, channel_list(args.use_tuner), channel_list(args.use_gracenote))
    log(f"Channels: {stats['gracenote']} from Gracenote, {stats['both']} from Gracenote "
        f"plus the tuner, {stats['tuner']} from the tuner only")

    images = Images(net, args.image_dir)
    images.fetch_all(channels)
    if args.image_dir:
        log(f"Images: {len(images.done) - images.failed} in {args.image_dir} "
            f"({images.fetched} new, {images.failed} couldn't be downloaded)")
    n_ch, n_prog = write_xmltv(args.output, channels, images, args.mark.lower())
    purge(args.cache, args.days)
    log(f"Wrote {args.output}: {n_ch} channels, {n_prog} programs, "
        f"{net.requests} requests, {time.time() - began:.0f} s")


def purge(cache, days):
    cutoff = time.time() - (days + 2) * 86400
    for name in os.listdir(cache):
        path = os.path.join(cache, name)
        if name.startswith("grid-") and os.path.getmtime(path) < cutoff:
            os.remove(path)
        elif name.startswith("series-") and os.path.getmtime(path) < time.time() - 4 * DETAILS_MAX_AGE:
            os.remove(path)


if __name__ == "__main__":
    main()
