# Entradas Boca - Multi-Container Launcher & Monitor Queue-it

Herramienta para abrir múltiples sesiones aisladas de **Boca Socios** en Mozilla Firefox (usando Multi-Account Containers) y monitorear en tiempo real el estado y tiempo de espera de todas las colas de **Queue-it** en simultáneo.

---

## 🚀 Funcionalidades

1. **Lanzador de Contenedores (`BOCA.bat` / `launch_browsers.py`)**:
   - Abre la cantidad de pestañas deseadas en Firefox.
   - Cada pestaña se crea en un contenedor independiente (`QA-01`, `QA-02`, ...), aislando cookies y sesiones para obtener identificadores de cola Queue-it únicos.

2. **Monitor en Tiempo Real (`MONITOR.bat` / `monitor_boca.py`)**:
   - Interfaz web local interactiva (`http://127.0.0.1:8765`).
   - Lee automáticamente las cookies y sesiones activas de Firefox sin bloquear el navegador.
   - Consulta de forma concurrente el tiempo de espera, porcentaje de avance y usuarios en cola de todas las pestañas.
   - **Protección de Redirección**: No consume ni roba el pase único de entrada en segundo plano (`isClientRedayToRedirect: False`).
   - **Auto-ordenamiento**: Muestra arriba de todo la cola con menor tiempo de espera.
   - **Alerta Sonora**: Notifica cuando una sesión está lista o con menos de 5 minutos de espera.
   - **Apertura Instantánea en 1 Clic**: Botón para abrir directamente el contenedor en Firefox con la sesión seleccionada.

---

## 📋 Requisitos Previos

1. **Mozilla Firefox** instalado.
2. Extensión **[Multi-Account Containers](https://addons.mozilla.org/firefox/addon/multi-account-containers/)**.
3. Extensión **[Open external links in a container](https://addons.mozilla.org/firefox/addon/open-url-in-container/)** (habilita el protocolo `ext+container:`).
4. Configuración en Firefox:
   - Ingresar a `about:config` en la barra de direcciones.
   - Buscar `network.protocol-handler.external.ext+container` y establecerlo en `true` (para evitar el diálogo de confirmación de Windows en cada pestaña).
5. **Python 3.x** instalado.

---

## 🛠️ Uso

### 1. Abrir las sesiones de Firefox
Ejecutar `BOCA.bat` (o `python launch_browsers.py`) e ingresar la cantidad de pestañas a abrir (por ejemplo `20` o `40`).

### 2. Iniciar el Monitor
Ejecutar `MONITOR.bat` (o `python monitor_boca.py`).
Se abrirá automáticamente la interfaz web en tu navegador (`http://127.0.0.1:8765`), mostrando todos los identificadores, su progreso y tiempo restante. Cuando veas uno con poco tiempo o en verde (`¡ES TU TURNO!`), hacé clic en **`🚀 Abrir Pestaña`**.
