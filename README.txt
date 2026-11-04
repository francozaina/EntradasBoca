SCRIPT_BOCA
===========

Abre varias pestanas de Firefox (BOCA Socios), cada una en un contenedor
distinto para no compartir cookies entre si.

REQUISITOS
----------
- Firefox instalado.
- Extension "Multi-Account Containers"
  https://addons.mozilla.org/firefox/addon/multi-account-containers/
- Extension "Open external links in a container"
  https://addons.mozilla.org/firefox/addon/open-url-in-container/
  (habilita el protocolo ext+container: que usa el script)
- En about:config, activar:
  network.protocol-handler.external.ext+container = true
  (si no, Windows muestra un dialogo "Elegir aplicacion" en cada pestana)

USO
---
Ejecutar BOCA.bat y escribir cuantas pestanas abrir.
