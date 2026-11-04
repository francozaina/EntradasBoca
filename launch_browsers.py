"""
launch_browsers.py
Abre 60 pestanas de Firefox, cada una en un contenedor (Multi-Account
Containers) distinto, para que no compartan cookies entre si.

Requiere en Firefox:
  - Extension "Multi-Account Containers" (contenedores nativos de Firefox)
  - Extension "Open external links in a container"
    (github.com/honsiorovskyi/open-url-in-container) -> expone el protocolo
    ext+container: que este script usa para crear y abrir cada contenedor
    desde linea de comandos.
  - about:config -> network.protocol-handler.external.ext+container = true
    (si no, Windows muestra un dialogo "Elegir aplicacion" en cada pestana)
"""

import os
import subprocess
import sys
import time
from urllib.parse import quote

URL = "https://bocasocios.bocajuniors.com.ar/home"
NUM_CONTAINERS = 10
CONTAINER_PREFIX = "QA"
DELAY_BETWEEN_TABS = 0.35  # seg; si van muy seguidas, Firefox descarta algunos comandos remotos

FIREFOX_PATHS = [
    r"C:\Program Files\Mozilla Firefox\firefox.exe",
    r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Mozilla Firefox\firefox.exe"),
]

COLORS = ["blue", "turquoise", "green", "yellow", "orange", "red", "pink", "purple"]
ICONS = [
    "fingerprint", "briefcase", "dollar", "cart", "circle", "gift",
    "vacation", "food", "fruit", "pet", "tree", "chill", "fence",
]


def find_firefox():
    for path in FIREFOX_PATHS:
        if os.path.isfile(path):
            return path
    return None


def container_link(index, url=URL):
    name = f"{CONTAINER_PREFIX}-{index:02d}"
    color = COLORS[index % len(COLORS)]
    icon = ICONS[index % len(ICONS)]
    return f"ext+container:name={quote(name)}&color={color}&icon={icon}&url={quote(url, safe='')}"


def main():
    exe = find_firefox()
    if not exe:
        print("Firefox no encontrado. Instalalo o corregi FIREFOX_PATHS.")
        sys.exit(1)

    print(f"Firefox: {exe}")
    
    while True:
        try:
            val = input("¿Cuántas pestañas quieres abrir? ")
            num_containers = int(val)
            if num_containers > 0:
                break
            print("Por favor ingresa un número mayor a 0.")
        except ValueError:
            print("Por favor ingresa un número válido.")
            
    print(f"Abriendo {num_containers} pestanas en contenedores separados ({CONTAINER_PREFIX}-01..{CONTAINER_PREFIX}-{num_containers:02d})...")

    # --- INICIO DEL FIX ---
    # Usamos DETACHED_PROCESS para que el editor/debugger no cierre Firefox al terminar el script
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen([exe], creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP)
    time.sleep(3)
    # --- FIN DEL FIX ---

    for i in range(1, num_containers + 1):
        link = container_link(i)
        subprocess.Popen([exe, link], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"  [{i}/{num_containers}] {CONTAINER_PREFIX}-{i:02d}")
        time.sleep(DELAY_BETWEEN_TABS)

    print("\nListo.")
    print("Si aparecio un dialogo 'Elegir aplicacion' en vez de abrir la pestana, activa:")
    print("  about:config -> network.protocol-handler.external.ext+container = true")


def _selftest():
    link = container_link(1, "https://example.com/x y")
    assert link == (
        "ext+container:name=QA-01&color=turquoise&icon=briefcase"
        "&url=https%3A%2F%2Fexample.com%2Fx%20y"
    ), link
    assert "name=QA-60" in container_link(60)
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        _selftest()
    else:
        main()
