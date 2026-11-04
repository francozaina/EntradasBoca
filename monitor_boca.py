"""
monitor_boca.py
Monitor en tiempo real de colas Queue-it (Boca Socios) para multiples
contenedores de Mozilla Firefox (Multi-Account Containers).

Lee automaticamente las sesiones y cookies de Queue-it desde el perfil activo
de Firefox (cookies.sqlite + sessionstore recovery.jsonlz4), consulta en
paralelo el estado y tiempo de espera de cada identificador de cola en Queue-it,
y permite abrir en 1 click cualquier pestaña/contenedor en Firefox.
"""

import glob
import json
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8765
DEFAULT_TARGET_URL = "https://bocasocios.bocajuniors.com.ar/home"
DEFAULT_GW_REDIRECT = "https://bocasocios-gw.bocajuniors.com.ar/queueit/redirect"

FIREFOX_PATHS = [
    r"C:\Program Files\Mozilla Firefox\firefox.exe",
    r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Mozilla Firefox\firefox.exe"),
]

# Mapeo de colores internos de Firefox a colores validos del protocolo ext+container:
EXT_COLOR_MAP = {
    "cyan": "turquoise",
    "turquoise": "turquoise",
    "blue": "blue",
    "green": "green",
    "yellow": "yellow",
    "orange": "orange",
    "red": "red",
    "pink": "pink",
    "purple": "purple",
    "violet": "purple",
    "gray": "blue",
    "toolbar": "blue",
}

# Cache en memoria para encabezados X-Queueit-Queueitem-V2 y ultimo estado
QUEUE_HEADER_CACHE = {}
CACHE_LOCK = threading.Lock()


def find_firefox():
    for path in FIREFOX_PATHS:
        if os.path.isfile(path):
            return path
    return None


def find_active_firefox_profile():
    profiles_dir = os.path.expandvars(r"%APPDATA%\Mozilla\Firefox\Profiles")
    if not os.path.isdir(profiles_dir):
        return None

    candidates = []
    for entry in os.listdir(profiles_dir):
        full_path = os.path.join(profiles_dir, entry)
        cookies_path = os.path.join(full_path, "cookies.sqlite")
        if os.path.isdir(full_path) and os.path.isfile(cookies_path):
            mtime = os.path.getmtime(cookies_path)
            wal_path = cookies_path + "-wal"
            if os.path.isfile(wal_path):
                mtime = max(mtime, os.path.getmtime(wal_path))
            candidates.append((mtime, full_path))

    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def decompress_mozlz4(data):
    if data[:8] != b"mozLz40\x00":
        raise ValueError("Formato mozLz4 invalido")
    src = memoryview(data)[12:]
    out = bytearray()
    pos = 0
    n = len(src)
    while pos < n:
        token = src[pos]
        pos += 1
        lit_len = token >> 4
        if lit_len == 15:
            while True:
                b = src[pos]
                pos += 1
                lit_len += b
                if b != 255:
                    break
        out.extend(src[pos : pos + lit_len])
        pos += lit_len
        if pos >= n:
            break
        offset = src[pos] | (src[pos + 1] << 8)
        pos += 2
        match_len = (token & 0x0F) + 4
        if (token & 0x0F) == 15:
            while True:
                b = src[pos]
                pos += 1
                match_len += b
                if b != 255:
                    break
        start = len(out) - offset
        for i in range(match_len):
            out.append(out[start + i])
    return bytes(out)


def read_firefox_containers(profile_dir):
    containers_file = os.path.join(profile_dir, "containers.json")
    ctx_map = {
        0: {
            "userContextId": 0,
            "name": "Sin Contenedor",
            "color": "blue",
            "icon": "circle",
        }
    }
    if not os.path.isfile(containers_file):
        return ctx_map

    try:
        with open(containers_file, encoding="utf-8") as f:
            data = json.load(f)
        for ident in data.get("identities", []):
            uid = ident.get("userContextId")
            if uid is None:
                continue
            name = ident.get("name") or ident.get("l10nId") or f"Contenedor-{uid}"
            ctx_map[int(uid)] = {
                "userContextId": int(uid),
                "name": name,
                "color": ident.get("color") or "blue",
                "icon": ident.get("icon") or "fingerprint",
            }
    except Exception:
        pass
    return ctx_map


def read_sessionstore_data(profile_dir):
    """
    Lee recovery.jsonlz4 para conocer:
    - Que pestañas estan abiertas y su numero de pestaña en Firefox
    - La URL exacta que tiene abierta cada contenedor
    - Las cookies de sesion en memoria (como Queue-it-visitorsession)
    """
    tabs_by_ctx = {}
    session_cookies_by_ctx = {}

    candidates = [
        os.path.join(profile_dir, "sessionstore-backups", "recovery.jsonlz4"),
        os.path.join(profile_dir, "sessionstore-backups", "recovery.baklz4"),
        os.path.join(profile_dir, "sessionstore.jsonlz4"),
    ]
    rec_path = next((p for p in candidates if os.path.isfile(p)), None)
    if not rec_path:
        return tabs_by_ctx, session_cookies_by_ctx

    try:
        with open(rec_path, "rb") as f:
            raw = decompress_mozlz4(f.read())
        sess = json.loads(raw.decode("utf-8", errors="replace"))

        for w_idx, win in enumerate(sess.get("windows", [])):
            for t_idx, tab in enumerate(win.get("tabs", [])):
                uctx = int(tab.get("userContextId", 0) or 0)
                entries = tab.get("entries", [])
                if not entries:
                    continue
                last_entry = entries[-1]
                url = last_entry.get("url", "")
                title = last_entry.get("title", "")
                if "queue-it.net" in url or "bocajuniors.com.ar" in url or uctx not in tabs_by_ctx:
                    tabs_by_ctx[uctx] = {
                        "window_index": w_idx + 1,
                        "tab_number": t_idx + 1,
                        "url": url,
                        "title": title,
                    }

        for sc in sess.get("cookies", []):
            host = sc.get("host", "")
            if "queue-it.net" in host:
                oa = sc.get("originAttributes", {})
                uctx = int(oa.get("userContextId", 0) or 0)
                name = sc.get("name", "")
                val = sc.get("value", "")
                if name and val:
                    session_cookies_by_ctx.setdefault(uctx, {})[name] = val
    except Exception:
        pass

    return tabs_by_ctx, session_cookies_by_ctx


def discover_firefox_queues():
    profile_dir = find_active_firefox_profile()
    if not profile_dir:
        return [], "No se encontro el perfil de Mozilla Firefox."

    ctx_map = read_firefox_containers(profile_dir)
    tabs_by_ctx, sess_cookies_by_ctx = read_sessionstore_data(profile_dir)

    tmp_dir = tempfile.mkdtemp(prefix="boca_ff_")
    try:
        cookies_src = os.path.join(profile_dir, "cookies.sqlite")
        cookies_dst = os.path.join(tmp_dir, "cookies.sqlite")
        shutil.copy2(cookies_src, cookies_dst)
        for ext in ("-wal", "-shm"):
            if os.path.isfile(cookies_src + ext):
                shutil.copy2(cookies_src + ext, cookies_dst + ext)

        conn = sqlite3.connect(cookies_dst)
        cur = conn.cursor()
        cur.execute(
            "SELECT host, name, value, originAttributes, lastAccessed "
            "FROM moz_cookies WHERE host LIKE '%queue-it.net%'"
        )
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return [], f"Error leyendo cookies de Firefox: {e}"
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    sessions = {}
    for host, name, val, oa, last_accessed in rows:
        m_ctx = re.search(r"userContextId=(\d+)", oa or "")
        cid = int(m_ctx.group(1)) if m_ctx else 0

        s = sessions.setdefault(
            cid,
            {
                "userContextId": cid,
                "host": host.lstrip("."),
                "cookies": {},
                "queue_id": None,
                "event_id": None,
                "customer_id": "bocajuniors",
                "last_accessed": 0,
            },
        )
        s["cookies"][name] = val
        if last_accessed and last_accessed > s["last_accessed"]:
            s["last_accessed"] = last_accessed

        # Ejemplo: Queue-it-bocajuniors_________e20261101ple -> Qid=2c9f00f6-...&Cid=es-ES&f=0
        m_ev = re.match(r"^Queue-it-([a-zA-Z0-9]+?)_+([a-zA-Z0-9]+)$", name)
        if m_ev:
            s["customer_id"] = m_ev.group(1)
            s["event_id"] = m_ev.group(2)
            m_qid = re.search(r"Qid=([0-9a-fA-F-]{36})", val)
            if m_qid:
                s["queue_id"] = m_qid.group(1)

    # Segundo pase: si alguna sesion tiene cookie Queue-it-<UUID> pero no tenia la cookie de evento
    default_event = next(
        (s["event_id"] for s in sessions.values() if s.get("event_id")), "e20261101ple"
    )
    for cid, s in sessions.items():
        if not s["event_id"]:
            s["event_id"] = default_event
        if not s["queue_id"]:
            for cname, cval in s["cookies"].items():
                m_uuid = re.match(r"^Queue-it-([0-9a-fA-F-]{36})$", cname)
                if m_uuid:
                    s["queue_id"] = m_uuid.group(1)
                    break

        # Agregar cookies de sesion desde recovery.jsonlz4 (ej. Queue-it-visitorsession)
        if cid in sess_cookies_by_ctx:
            for k, v in sess_cookies_by_ctx[cid].items():
                s["cookies"].setdefault(k, v)

        c_info = ctx_map.get(
            cid,
            {
                "userContextId": cid,
                "name": f"Contenedor-{cid}",
                "color": "blue",
                "icon": "fingerprint",
            },
        )
        s["container_name"] = c_info["name"]
        s["container_color"] = c_info["color"]
        s["container_icon"] = c_info["icon"]

        tab_info = tabs_by_ctx.get(cid, {})
        s["tab_number"] = tab_info.get("tab_number")
        s["tab_url"] = tab_info.get("url")
        s["tab_title"] = tab_info.get("title")
        s["is_open_in_firefox"] = cid in tabs_by_ctx

    valid_sessions = [s for s in sessions.values() if s.get("queue_id")]
    return valid_sessions, None


def parse_wait_seconds(which_is_in, expected_utc, seconds_to_start, is_before, is_ready):
    """
    Convierte el estado y texto de espera de Queue-it en segundos estimados
    para poder ordenar automaticamente de menor a mayor tiempo de espera.
    """
    if is_ready:
        return 0

    # Si Queue-it informa expectedServiceTimeUTC, calculamos la diferencia real
    if expected_utc:
        try:
            clean_iso = re.sub(r"\.\d+Z$", "Z", expected_utc)
            if clean_iso.endswith("Z"):
                from datetime import datetime, timezone

                dt = datetime.strptime(clean_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
                diff = (dt - datetime.now(timezone.utc)).total_seconds()
                return max(1, int(diff))
        except Exception:
            pass

    if which_is_in:
        txt = which_is_in.lower().strip()
        if "menos de un minuto" in txt or "less than a minute" in txt or "un minuto" == txt:
            return 30
        if "más de una hora" in txt or "mas de una hora" in txt or "more than an hour" in txt:
            return 3601
        m_min = re.search(r"(\d+)\s*minuto", txt)
        m_hr = re.search(r"(\d+)\s*hora", txt)
        if m_hr or m_min:
            total = 0
            if m_hr:
                total += int(m_hr.group(1)) * 3600
            if m_min:
                total += int(m_min.group(1)) * 60
            return max(1, total)
        m_sec = re.search(r"(\d+)\s*segundo", txt)
        if m_sec:
            return max(1, int(m_sec.group(1)))

    if is_before:
        return 500000

    return 400000


def fetch_queue_status(session):
    qid = session["queue_id"]
    ev = session["event_id"]
    cust = session["customer_id"]
    host = session["host"] or "bocajuniors.queue-it.net"
    cid = session["userContextId"]

    cookie_hdr = "; ".join(f"{k}={v}" for k, v in session["cookies"].items())
    status_url = (
        f"https://{host}/spa-api/queue/{cust}/{ev}/{qid}/status"
        f"?cid=es-ES&l={urllib.parse.quote('Boca Socios')}"
    )

    # IMPORTANTE: isClientRedayToRedirect=False simula una pestaña en segundo plano,
    # de modo que el monitor NUNCA consuma ni robe la redireccion de la pestaña real.
    payload = json.dumps(
        {
            "targetUrl": DEFAULT_GW_REDIRECT,
            "customUrlParams": "",
            "layoutVersion": 181086721624,
            "layoutName": "Boca Socios",
            "isClientRedayToRedirect": False,
            "isBeforeOrIdle": False,
        }
    ).encode("utf-8")

    headers = {
        "Cookie": cookie_hdr,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) "
            "Gecko/20100101 Firefox/131.0"
        ),
        "Origin": f"https://{host}",
        "Referer": f"https://{host}/?c={cust}&e={ev}",
    }

    with CACHE_LOCK:
        cached_item_hdr = QUEUE_HEADER_CACHE.get(qid)
    if cached_item_hdr:
        headers["X-Queueit-Queueitem-V2"] = cached_item_hdr

    t_start = time.time()
    try:
        req = urllib.request.Request(status_url, data=payload, method="POST", headers=headers)
        with urllib.request.urlopen(req, timeout=8) as resp:
            q_hdr = resp.headers.get("X-Queueit-Queueitem-V2")
            if q_hdr:
                with CACHE_LOCK:
                    QUEUE_HEADER_CACHE[qid] = q_hdr
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return {
            "userContextId": cid,
            "container_name": session["container_name"],
            "container_color": session["container_color"],
            "container_icon": session["container_icon"],
            "tab_number": session["tab_number"],
            "is_open_in_firefox": session["is_open_in_firefox"],
            "queue_id": qid,
            "event_id": ev,
            "customer_id": cust,
            "status_type": "error",
            "status_label": "Error de conexión",
            "which_is_in": str(e),
            "expected_service_time": "-",
            "users_ahead": None,
            "queue_number": None,
            "progress": 0,
            "seconds_to_start": None,
            "event_start_time": None,
            "sort_seconds": 999999,
            "direct_url": f"https://{host}/?c={cust}&e={ev}&q={qid}&cid=es-ES",
            "tab_url": session.get("tab_url") or f"https://{host}/?c={cust}&e={ev}&q={qid}&cid=es-ES",
            "was_redirected": "WasRedirected=true" in session["cookies"].get(f"Queue-it-{qid}", ""),
        }

    ticket = data.get("ticket") or {}
    layout = data.get("layout") or {}
    is_before = bool(data.get("isBeforeOrIdle"))
    page_id = data.get("pageId") or ("before" if is_before else "queue")
    forecast_status = data.get("forecastStatus") or ""
    redirect_url = data.get("redirectUrl")

    which_is_in = ticket.get("whichIsIn") or "-"
    expected_time = ticket.get("expectedServiceTime") or "-"
    expected_utc = ticket.get("expectedServiceTimeUTC")
    users_ahead = ticket.get("usersInLineAheadOfYou")
    queue_number = ticket.get("queueNumber")
    progress_raw = ticket.get("progress")
    progress_pct = round(float(progress_raw) * 100, 1) if progress_raw is not None else 0.0
    seconds_to_start = ticket.get("secondsToStart")
    event_start_fmt = ticket.get("eventStartTimeFormatted")
    queue_paused = bool(ticket.get("queuePaused") or layout.get("queueIsPausedVisible"))

    first_in_line = bool(
        redirect_url
        or layout.get("firstInLineVisible")
        or layout.get("servicedSoonVisible")
        or forecast_status.lower() == "firstinline"
        or (progress_raw is not None and float(progress_raw) >= 1.0)
    )

    was_redirected = "WasRedirected=true" in session["cookies"].get(f"Queue-it-{qid}", "")

    sort_seconds = parse_wait_seconds(
        which_is_in, expected_utc, seconds_to_start, is_before, first_in_line
    )

    if first_in_line:
        status_type = "ready"
        status_label = "¡ES TU TURNO!"
        progress_pct = 100.0
        if which_is_in in ("-", "calculando..."):
            which_is_in = "¡Listo para entrar!"
    elif queue_paused:
        status_type = "paused"
        status_label = "Cola Pausada"
    elif is_before:
        status_type = "before"
        status_label = f"Pre-Cola ({event_start_fmt or '13:00'} hs)"
    elif sort_seconds <= 300:
        status_type = "soon"
        status_label = "¡Poco Tiempo!"
    elif sort_seconds <= 900:
        status_type = "medium"
        status_label = "En Cola (Rápida)"
    else:
        status_type = "queue"
        status_label = "En Cola"

    direct_url = f"https://{host}/?c={cust}&e={ev}&q={qid}&cid=es-ES"

    return {
        "userContextId": cid,
        "container_name": session["container_name"],
        "container_color": session["container_color"],
        "container_icon": session["container_icon"],
        "tab_number": session["tab_number"],
        "is_open_in_firefox": session["is_open_in_firefox"],
        "queue_id": qid,
        "event_id": ev,
        "customer_id": cust,
        "page_id": page_id,
        "forecast_status": forecast_status,
        "status_type": status_type,
        "status_label": status_label,
        "which_is_in": which_is_in,
        "expected_service_time": expected_time,
        "users_ahead": users_ahead,
        "queue_number": queue_number,
        "progress": progress_pct,
        "seconds_to_start": seconds_to_start,
        "event_start_time": event_start_fmt,
        "sort_seconds": sort_seconds,
        "was_redirected": was_redirected,
        "redirect_url": redirect_url,
        "direct_url": direct_url,
        "tab_url": session.get("tab_url") or direct_url,
        "latency_ms": int((time.time() - t_start) * 1000),
    }


def scan_all_queues():
    t0 = time.time()
    sessions, err = discover_firefox_queues()
    if err:
        return {
            "ok": False,
            "error": err,
            "items": [],
            "scan_time_ms": int((time.time() - t0) * 1000),
            "timestamp": time.strftime("%H:%M:%S"),
        }

    if not sessions:
        return {
            "ok": True,
            "error": None,
            "items": [],
            "scan_time_ms": int((time.time() - t0) * 1000),
            "timestamp": time.strftime("%H:%M:%S"),
        }

    with ThreadPoolExecutor(max_workers=24) as executor:
        results = list(executor.map(fetch_queue_status, sessions))

    # Ordenar por menor tiempo de espera primero, luego mayor progreso, luego nombre de contenedor
    def sort_key(item):
        m_num = re.search(r"(\d+)", item.get("container_name") or "")
        c_num = int(m_num.group(1)) if m_num else 9999
        return (
            item.get("sort_seconds", 999999),
            -float(item.get("progress") or 0),
            item.get("tab_number") or 9999,
            c_num,
        )

    results.sort(key=sort_key)

    return {
        "ok": True,
        "error": None,
        "items": results,
        "total": len(results),
        "open_tabs_count": sum(1 for r in results if r.get("is_open_in_firefox")),
        "ready_count": sum(1 for r in results if r.get("status_type") == "ready"),
        "soon_count": sum(1 for r in results if r.get("status_type") in ("ready", "soon")),
        "scan_time_ms": int((time.time() - t0) * 1000),
        "timestamp": time.strftime("%H:%M:%S"),
    }


def open_in_firefox_container(container_name, color, icon, target_url):
    exe = find_firefox()
    if not exe:
        return False, "No se encontro firefox.exe"

    ext_color = EXT_COLOR_MAP.get((color or "").lower(), "blue")
    ext_icon = icon or "fingerprint"

    if not container_name or container_name == "Sin Contenedor":
        cmd = [exe, target_url]
    else:
        ext_uri = (
            f"ext+container:name={urllib.parse.quote(container_name)}"
            f"&color={ext_color}&icon={ext_icon}"
            f"&url={urllib.parse.quote(target_url, safe='')}"
        )
        cmd = [exe, ext_uri]

    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, f"Abierto en Firefox ({container_name})"
    except Exception as e:
        return False, str(e)


HTML_PAGE = r"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Monitor Queue-it | Boca Socios</title>
<style>
  :root {
    --boca-blue-dark: #001433;
    --boca-blue: #002766;
    --boca-blue-light: #0a3d91;
    --boca-gold: #f5c400;
    --boca-gold-bright: #ffd633;
    --bg-dark: #070d19;
    --card-bg: #0f1b33;
    --card-border: #1e335c;
    --text-main: #f0f4f8;
    --text-muted: #94a3b8;
    --green: #10b981;
    --amber: #f59e0b;
    --red: #ef4444;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
    background: radial-gradient(circle at top, var(--boca-blue-dark) 0%, var(--bg-dark) 100%);
    color: var(--text-main);
    min-height: 100vh;
    padding: 18px 24px;
  }
  header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    background: linear-gradient(90deg, var(--boca-blue) 0%, #001f52 50%, var(--boca-blue) 100%);
    border: 2px solid var(--boca-gold);
    border-radius: 14px;
    padding: 16px 22px;
    box-shadow: 0 10px 30px rgba(0,0,0,0.45);
    margin-bottom: 18px;
  }
  .brand {
    display: flex;
    align-items: center;
    gap: 14px;
  }
  .shield {
    width: 44px;
    height: 44px;
    border-radius: 10px;
    background: var(--boca-blue-dark);
    border: 2px solid var(--boca-gold);
    display: flex;
    flex-direction: column;
    overflow: hidden;
    font-weight: 900;
    font-size: 11px;
    text-align: center;
    box-shadow: 0 4px 12px rgba(245,196,0,0.25);
  }
  .shield div { flex: 1; display: flex; align-items: center; justify-content: center; }
  .shield .s-blue { background: #002b7f; color: var(--boca-gold); }
  .shield .s-gold { background: var(--boca-gold); color: #002b7f; }
  .brand h1 {
    font-size: 20px;
    font-weight: 800;
    letter-spacing: 0.4px;
    color: #fff;
  }
  .brand h1 span { color: var(--boca-gold); }
  .brand p { font-size: 12.5px; color: #cbd5e1; margin-top: 2px; }

  .controls {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 10px;
  }
  button, select, input {
    font-family: inherit;
    font-size: 13.5px;
    border-radius: 8px;
  }
  .btn {
    cursor: pointer;
    border: none;
    padding: 9px 15px;
    font-weight: 700;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    transition: all 0.15s ease;
  }
  .btn-gold {
    background: var(--boca-gold);
    color: #001a4d;
    box-shadow: 0 3px 12px rgba(245, 196, 0, 0.3);
  }
  .btn-gold:hover { background: var(--boca-gold-bright); transform: translateY(-1px); }
  .btn-green {
    background: var(--green);
    color: #032218;
    box-shadow: 0 3px 12px rgba(16, 185, 129, 0.35);
  }
  .btn-green:hover { filter: brightness(1.1); transform: translateY(-1px); }
  .btn-outline {
    background: rgba(255,255,255,0.07);
    color: #e2e8f0;
    border: 1px solid rgba(255,255,255,0.18);
  }
  .btn-outline:hover { background: rgba(255,255,255,0.14); }
  .btn-sm { padding: 6px 11px; font-size: 12.5px; border-radius: 6px; }

  select, input[type="text"] {
    background: rgba(7, 13, 25, 0.75);
    color: #fff;
    border: 1px solid rgba(255,255,255,0.22);
    padding: 8px 12px;
    outline: none;
  }
  select:focus, input[type="text"]:focus { border-color: var(--boca-gold); }

  /* Stats & Best Banner */
  .top-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 12px;
    margin-bottom: 16px;
  }
  .stat-card {
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    border-radius: 11px;
    padding: 13px 16px;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
  }
  .stat-card .label { font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.6px; }
  .stat-card .value { font-size: 24px; font-weight: 800; margin-top: 4px; color: #fff; }
  .stat-card.highlight {
    border-color: var(--boca-gold);
    background: linear-gradient(135deg, rgba(245,196,0,0.14) 0%, var(--card-bg) 100%);
  }
  .stat-card.ready-highlight {
    border-color: var(--green);
    background: linear-gradient(135deg, rgba(16,185,129,0.18) 0%, var(--card-bg) 100%);
  }

  /* Best Ticket Spotlight */
  #bestSpotlight {
    display: none;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 14px;
    background: linear-gradient(90deg, rgba(16,185,129,0.2) 0%, rgba(245,196,0,0.15) 100%);
    border: 2px solid var(--boca-gold);
    border-radius: 12px;
    padding: 14px 20px;
    margin-bottom: 16px;
  }
  .spotlight-info { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
  .spotlight-badge {
    background: var(--boca-gold);
    color: #001a4d;
    font-weight: 900;
    padding: 5px 10px;
    border-radius: 6px;
    font-size: 12px;
    text-transform: uppercase;
  }

  /* Filter bar */
  .toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 12px;
    background: rgba(15, 27, 51, 0.75);
    border: 1px solid var(--card-border);
    padding: 10px 16px;
    border-radius: 10px;
  }
  .toolbar-left, .toolbar-right {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }

  /* Table */
  .table-wrap {
    background: var(--card-bg);
    border: 1px solid var(--card-border);
    border-radius: 12px;
    overflow-x: auto;
    box-shadow: 0 8px 24px rgba(0,0,0,0.35);
  }
  table {
    width: 100%;
    border-collapse: collapse;
    text-align: left;
  }
  th {
    background: rgba(0, 20, 51, 0.85);
    color: var(--boca-gold);
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    padding: 12px 14px;
    border-bottom: 1px solid var(--card-border);
    cursor: pointer;
    user-select: none;
    white-space: nowrap;
  }
  th:hover { color: #fff; }
  td {
    padding: 11px 14px;
    border-bottom: 1px solid rgba(255,255,255,0.06);
    font-size: 13.5px;
    vertical-align: middle;
  }
  tr:hover td { background: rgba(255,255,255,0.04); }
  tr.row-ready td {
    background: rgba(16, 185, 129, 0.16) !important;
    border-top: 1px solid rgba(16, 185, 129, 0.45);
    border-bottom: 1px solid rgba(16, 185, 129, 0.45);
  }
  tr.row-soon td {
    background: rgba(245, 196, 0, 0.11) !important;
  }

  /* Container pill */
  .container-pill {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    font-weight: 700;
    padding: 4px 10px;
    border-radius: 6px;
    background: rgba(255,255,255,0.07);
    border-left: 4px solid #3b82f6;
  }
  .color-blue { border-left-color: #3b82f6; }
  .color-turquoise, .color-cyan { border-left-color: #06b6d4; }
  .color-green { border-left-color: #10b981; }
  .color-yellow { border-left-color: #eab308; }
  .color-orange { border-left-color: #f97316; }
  .color-red { border-left-color: #ef4444; }
  .color-pink { border-left-color: #ec4899; }
  .color-purple, .color-violet { border-left-color: #a855f7; }

  .qid-code {
    font-family: 'Cascadia Mono', 'Consolas', monospace;
    font-size: 12.5px;
    color: #e2e8f0;
    background: rgba(0,0,0,0.35);
    padding: 4px 8px;
    border-radius: 5px;
    border: 1px solid rgba(255,255,255,0.09);
  }

  /* Badges */
  .badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 12px;
    font-weight: 700;
    white-space: nowrap;
  }
  .badge-ready { background: rgba(16, 185, 129, 0.25); color: #34d399; border: 1px solid #10b981; animation: pulse 1.2s infinite; }
  .badge-soon { background: rgba(245, 196, 0, 0.22); color: #fde047; border: 1px solid #f5c400; }
  .badge-medium { background: rgba(59, 130, 246, 0.22); color: #93c5fd; border: 1px solid #3b82f6; }
  .badge-queue { background: rgba(148, 163, 184, 0.16); color: #cbd5e1; border: 1px solid rgba(255,255,255,0.18); }
  .badge-before { background: rgba(245, 158, 11, 0.18); color: #fcd34d; border: 1px solid rgba(245, 158, 11, 0.4); }
  .badge-paused, .badge-error { background: rgba(239, 68, 68, 0.2); color: #fca5a5; border: 1px solid #ef4444; }

  @keyframes pulse {
    0%, 100% { transform: scale(1); }
    50% { transform: scale(1.04); }
  }

  .wait-time {
    font-size: 15px;
    font-weight: 800;
  }
  .wait-ready { color: #34d399; }
  .wait-soon { color: var(--boca-gold-bright); }

  /* Progress bar */
  .prog-wrap {
    width: 110px;
    background: rgba(255,255,255,0.1);
    height: 9px;
    border-radius: 99px;
    overflow: hidden;
    display: inline-block;
    vertical-align: middle;
    margin-right: 7px;
  }
  .prog-bar {
    height: 100%;
    background: linear-gradient(90deg, #3b82f6, var(--boca-gold));
    border-radius: 99px;
    transition: width 0.3s ease;
  }
  .prog-bar.prog-ready { background: var(--green); }

  .toast {
    position: fixed;
    bottom: 20px;
    right: 20px;
    background: var(--boca-blue);
    border: 2px solid var(--boca-gold);
    color: #fff;
    padding: 12px 18px;
    border-radius: 10px;
    font-weight: 700;
    box-shadow: 0 8px 25px rgba(0,0,0,0.5);
    display: none;
    z-index: 9999;
  }
</style>
</head>
<body>

<header>
  <div class="brand">
    <div class="shield">
      <div class="s-blue">C A</div>
      <div class="s-gold">B J</div>
      <div class="s-blue">★ ★</div>
    </div>
    <div>
      <h1>MONITOR <span>BOCA SOCIOS</span> QUEUE-IT</h1>
      <p id="subStatus">Escaneando contenedores de Firefox en tiempo real...</p>
    </div>
  </div>
  <div class="controls">
    <button class="btn btn-gold" id="btnRefresh" onclick="scanNow()">🔄 Escanear Ahora</button>
    <label style="font-size:13px; color:#cbd5e1;">Auto-Actualizar:</label>
    <select id="selInterval" onchange="updateTimer()">
      <option value="4000">Cada 4 seg</option>
      <option value="7000" selected>Cada 7 seg</option>
      <option value="12000">Cada 12 seg</option>
      <option value="25000">Cada 25 seg</option>
      <option value="0">Pausado</option>
    </select>
    <label style="display:flex; align-items:center; gap:5px; font-size:13px; cursor:pointer;">
      <input type="checkbox" id="chkSound" checked> 🔔 Alerta Sonora
    </label>
  </div>
</header>

<div class="top-grid">
  <div class="stat-card">
    <span class="label">Sesiones Queue-it Activas</span>
    <span class="value" id="stTotal">-</span>
  </div>
  <div class="stat-card">
    <span class="label">Estado del Evento</span>
    <span class="value" id="stPhase" style="font-size:18px;">-</span>
  </div>
  <div class="stat-card ready-highlight">
    <span class="label">¡Listas / Poco Tiempo (&lt;5m)!</span>
    <span class="value" id="stSoon" style="color:#34d399;">0</span>
  </div>
  <div class="stat-card highlight">
    <span class="label">Mejor Tiempo de Espera</span>
    <span class="value" id="stBest" style="color:var(--boca-gold); font-size:19px;">-</span>
  </div>
</div>

<div id="bestSpotlight">
  <div class="spotlight-info">
    <span class="spotlight-badge">⭐ MEJOR COLA DETECTADA</span>
    <span id="spotText" style="font-weight:700; font-size:15px;"></span>
  </div>
  <div style="display:flex; gap:8px;">
    <button class="btn btn-green" id="spotOpenBtn">🚀 Abrir Esta Pestaña en Firefox</button>
  </div>
</div>

<div class="toolbar">
  <div class="toolbar-left">
    <input type="text" id="txtSearch" placeholder="🔍 Buscar por ID (ej. 2c9f00f6) o contenedor (QA-01)..." style="width:320px;" oninput="renderTable()">
    <label style="display:flex; align-items:center; gap:6px; font-size:13px; cursor:pointer;">
      <input type="checkbox" id="chkOnlyOpen" onchange="renderTable()"> Mostrar solo pestañas abiertas actualmente
    </label>
  </div>
  <div class="toolbar-right">
    <span style="font-size:12.5px; color:var(--text-muted);">Ordenar por:</span>
    <select id="selSort" onchange="renderTable()">
      <option value="wait" selected>⚡ Menor Tiempo de Espera (Automático)</option>
      <option value="progress">📈 Mayor Progreso (%)</option>
      <option value="tab">🔢 Número de Pestaña / Contenedor</option>
    </select>
  </div>
</div>

<div class="table-wrap">
  <table>
    <thead>
      <tr>
        <th onclick="setSort('tab')"># Pestaña</th>
        <th onclick="setSort('tab')">Contenedor Firefox</th>
        <th>Identificador de Cola (Queue ID)</th>
        <th onclick="setSort('wait')">Estado</th>
        <th onclick="setSort('wait')">Tiempo de Espera</th>
        <th onclick="setSort('wait')">Llegada Est.</th>
        <th onclick="setSort('progress')">Progreso / Fila</th>
        <th style="text-align:right;">Acción Instantánea</th>
      </tr>
    </thead>
    <tbody id="tbRows">
      <tr><td colspan="8" style="text-align:center; padding:30px; color:#94a3b8;">Cargando sesiones de Firefox...</td></tr>
    </tbody>
  </table>
</div>

<div class="toast" id="toast"></div>

<script>
let currentItems = [];
let timerId = null;
let isScanning = false;
let lastBeepTime = 0;

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.display = 'block';
  setTimeout(() => { t.style.display = 'none'; }, 3000);
}

function playAlertBeep() {
  if (!document.getElementById('chkSound').checked) return;
  const now = Date.now();
  if (now - lastBeepTime < 8000) return;
  lastBeepTime = now;
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    [587.33, 880, 1174.66].forEach((freq, idx) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = 'triangle';
      osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.18, ctx.currentTime + idx * 0.14);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + idx * 0.14 + 0.25);
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.start(ctx.currentTime + idx * 0.14);
      osc.stop(ctx.currentTime + idx * 0.14 + 0.25);
    });
  } catch (e) {}
}

function formatCountdown(sec) {
  if (sec === null || sec === undefined) return '';
  sec = Math.max(0, parseInt(sec, 10));
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}m ${s < 10 ? '0' : ''}${s}s`;
}

async function scanNow() {
  if (isScanning) return;
  isScanning = true;
  const btn = document.getElementById('btnRefresh');
  btn.textContent = '⏳ Escaneando...';
  btn.disabled = true;

  try {
    const resp = await fetch('/api/status');
    const data = await resp.json();
    if (!data.ok) {
      document.getElementById('subStatus').textContent = '⚠️ ' + (data.error || 'Error');
    } else {
      currentItems = data.items || [];
      document.getElementById('stTotal').textContent = `${data.total} (${data.open_tabs_count} pestañas)`;
      document.getElementById('stSoon').textContent = data.soon_count || 0;
      document.getElementById('subStatus').textContent =
        `Último escaneo: ${data.timestamp} (${data.scan_time_ms} ms) — Evento: ${currentItems[0]?.event_id || '-'}`;

      if (currentItems.length > 0) {
        const first = currentItems[0];
        if (first.status_type === 'before') {
          document.getElementById('stPhase').textContent =
            `⏳ Pre-Cola (Inicia ${first.event_start_time || '13:00'} hs — faltan ${formatCountdown(first.seconds_to_start)})`;
        } else {
          document.getElementById('stPhase').textContent = '🟢 FILA ACTIVA EN CURSO';
        }

        document.getElementById('stBest').textContent =
          `${first.which_is_in} (${first.container_name})`;

        const spot = document.getElementById('bestSpotlight');
        if (first.status_type !== 'before') {
          spot.style.display = 'flex';
          document.getElementById('spotText').textContent =
            `${first.container_name} (Pestaña #${first.tab_number || '?'}) — ID: ${first.queue_id} — Espera: ${first.which_is_in} (Llegada: ${first.expected_service_time})`;
          document.getElementById('spotOpenBtn').onclick = () => openContainer(first);
        } else {
          spot.style.display = 'none';
        }

        if (data.soon_count > 0) {
          playAlertBeep();
        }
      }
      renderTable();
    }
  } catch (e) {
    document.getElementById('subStatus').textContent = '⚠️ Error conectando con el servidor local: ' + e;
  } finally {
    isScanning = false;
    btn.textContent = '🔄 Escanear Ahora';
    btn.disabled = false;
  }
}

function setSort(mode) {
  document.getElementById('selSort').value = mode;
  renderTable();
}

function renderTable() {
  const q = (document.getElementById('txtSearch').value || '').toLowerCase().trim();
  const onlyOpen = document.getElementById('chkOnlyOpen').checked;
  const sortMode = document.getElementById('selSort').value;

  let list = currentItems.filter(item => {
    if (onlyOpen && !item.is_open_in_firefox) return false;
    if (!q) return true;
    return (
      (item.queue_id || '').toLowerCase().includes(q) ||
      (item.container_name || '').toLowerCase().includes(q) ||
      (item.which_is_in || '').toLowerCase().includes(q) ||
      String(item.tab_number || '').includes(q)
    );
  });

  list.sort((a, b) => {
    if (sortMode === 'progress') {
      if ((b.progress || 0) !== (a.progress || 0)) return (b.progress || 0) - (a.progress || 0);
      return (a.sort_seconds || 999999) - (b.sort_seconds || 999999);
    }
    if (sortMode === 'tab') {
      const ta = a.tab_number || 9999;
      const tb = b.tab_number || 9999;
      if (ta !== tb) return ta - tb;
      return (a.container_name || '').localeCompare(b.container_name || '', undefined, {numeric: true});
    }
    // Default: menor tiempo de espera
    if ((a.sort_seconds || 999999) !== (b.sort_seconds || 999999)) {
      return (a.sort_seconds || 999999) - (b.sort_seconds || 999999);
    }
    if ((b.progress || 0) !== (a.progress || 0)) return (b.progress || 0) - (a.progress || 0);
    return (a.tab_number || 9999) - (b.tab_number || 9999);
  });

  const tbody = document.getElementById('tbRows');
  if (list.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; padding:26px; color:#94a3b8;">No se encontraron identificadores con ese filtro.</td></tr>';
    return;
  }

  tbody.innerHTML = list.map((item, idx) => {
    const rowClass = item.status_type === 'ready' ? 'row-ready' : (item.status_type === 'soon' ? 'row-soon' : '');
    const badgeClass = 'badge-' + (item.status_type || 'queue');
    const waitClass = item.status_type === 'ready' ? 'wait-ready' : (item.status_type === 'soon' ? 'wait-soon' : '');

    let waitDisplay = item.which_is_in || '-';
    if (item.status_type === 'before' && item.seconds_to_start !== null) {
      waitDisplay = `En ${formatCountdown(item.seconds_to_start)} (Calculando...)`;
    }

    let extraQueueInfo = `${item.progress || 0}%`;
    if (item.users_ahead !== null && item.users_ahead !== undefined) {
      extraQueueInfo += ` • ${item.users_ahead} delante`;
    } else if (item.queue_number !== null && item.queue_number !== undefined) {
      extraQueueInfo += ` • Pos #${item.queue_number}`;
    }

    const tabLabel = item.tab_number ? `Pestaña #${item.tab_number}` : 'Guardada';
    const itemJson = encodeURIComponent(JSON.stringify(item));

    return `<tr class="${rowClass}">
      <td style="font-weight:700; color:#cbd5e1;">${tabLabel}</td>
      <td>
        <span class="container-pill color-${item.container_color || 'blue'}">
          ${item.container_name}
        </span>
      </td>
      <td>
        <span class="qid-code">${item.queue_id}</span>
      </td>
      <td>
        <span class="badge ${badgeClass}">${item.status_label}</span>
      </td>
      <td>
        <span class="wait-time ${waitClass}">${waitDisplay}</span>
      </td>
      <td style="font-weight:700;">${item.expected_service_time || '-'}</td>
      <td>
        <div class="prog-wrap">
          <div class="prog-bar ${item.status_type === 'ready' ? 'prog-ready' : ''}" style="width:${Math.min(100, Math.max(4, item.progress || 0))}%"></div>
        </div>
        <span style="font-size:12px; color:#cbd5e1;">${extraQueueInfo}</span>
      </td>
      <td style="text-align:right; white-space:nowrap;">
        <button class="btn btn-gold btn-sm" onclick="openByEncoded('${itemJson}')">🚀 Abrir Pestaña</button>
        <button class="btn btn-outline btn-sm" onclick="copyLink('${item.direct_url}', '${item.queue_id}')" title="Copiar enlace directo de este Queue ID">📋 Copiar ID/Link</button>
      </td>
    </tr>`;
  }).join('');
}

function openByEncoded(enc) {
  const item = JSON.parse(decodeURIComponent(enc));
  openContainer(item);
}

async function openContainer(item) {
  showToast(`🚀 Abriendo ${item.container_name} (${item.queue_id.slice(0,8)}...) en Firefox...`);
  try {
    const resp = await fetch('/api/open', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        container_name: item.container_name,
        container_color: item.container_color,
        container_icon: item.container_icon,
        url: item.tab_url || item.direct_url,
        queue_id: item.queue_id
      })
    });
    const res = await resp.json();
    if (res.ok) {
      showToast(`✅ Pestaña abierta en contenedor ${item.container_name} (ID: ${item.queue_id.slice(0,8)}...)`);
    } else {
      showToast(`⚠️ Error: ${res.message}`);
    }
  } catch (e) {
    showToast('⚠️ Error al abrir en Firefox: ' + e);
  }
}

function copyLink(url, qid) {
  navigator.clipboard.writeText(url).then(() => {
    showToast(`📋 Copiado link de cola (${qid})`);
  });
}

function updateTimer() {
  if (timerId) clearInterval(timerId);
  const ms = parseInt(document.getElementById('selInterval').value, 10);
  if (ms > 0) {
    timerId = setInterval(scanNow, ms);
  }
}

scanNow();
updateTimer();
</script>
</body>
</html>
"""


class MonitorHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Silenciar logs HTTP por cada request

    def _send_json(self, obj, status=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            raw = HTML_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        elif self.path.startswith("/api/status"):
            data = scan_all_queues()
            self._send_json(data)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/open":
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            c_name = body.get("container_name")
            c_color = body.get("container_color")
            c_icon = body.get("container_icon")
            url = body.get("url") or DEFAULT_TARGET_URL
            ok, msg = open_in_firefox_container(c_name, c_color, c_icon, url)
            self._send_json({"ok": ok, "message": msg})
        else:
            self.send_error(404)


def main():
    if "--test" in sys.argv:
        res = scan_all_queues()
        print(f"OK={res['ok']} | Total={res.get('total')} | ScanMs={res.get('scan_time_ms')}")
        for it in res.get("items", [])[:5]:
            print(" ", it["container_name"], it["tab_number"], it["queue_id"], it["status_label"], it["which_is_in"])
        return

    server = ThreadingHTTPServer(("127.0.0.1", PORT), MonitorHandler)
    url = f"http://127.0.0.1:{PORT}"
    print("=" * 62)
    print("  MONITOR BOCA SOCIOS QUEUE-IT ACTIVO")
    print(f"  Interfaz web: {url}")
    print("=" * 62)
    print("Abriendo interfaz en el navegador... (No cierres esta consola)")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCerrando monitor...")
        server.server_close()


if __name__ == "__main__":
    main()
