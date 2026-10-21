# Bot de fallas — Centrality → Telegram / Teams

Cuando aparece una falla en Centrality, el bot avisa **qué calles están
afectadas y cuántos clientes**, y después avisa si **escala**, si hay
**reposición parcial** y cuando se **normaliza**. Sin spam, sin datos
personales y sin guardar credenciales de SAESA en ningún lado.

```
 Centrality (sesión del despachador, Chrome)
        │  la extensión ejecuta reportes / lee la bandeja visible
        ▼
 ┌──────────────────────┐   POST CSV/JSON    ┌──────────────────────────┐
 │ Extensión Chrome MV3 │ ─────────────────► │ Bot local (Python stdlib) │
 │ - cada 5 min         │   127.0.0.1:8771   │ - une fuentes por CASO    │
 │ - usa TU sesión      │                    │ - calles + clientes       │
 │ - sin credenciales   │                    │ - detecta nueva/escala/   │
 └──────────────────────┘                    │   normalizada             │
                                             └────────────┬─────────────┘
                                                          │ HTTPS saliente
                                                          ▼
                                                Telegram / Teams / archivo
```

Es el mismo diseño que ya se validó en producción en el motor de despacho
(`despacho_automatico/produccion`): la descarga vive en la pestaña de
Centrality porque el *service worker* de MV3 muere en descargas largas, y el
servicio local sólo recibe el CSV.

---

## 1. De dónde salen las calles y los clientes (leer antes de confiar en un número)

| Dato | Fuente | Qué significa realmente |
|---|---|---|
| **Clientes afectados** | *Informe de casos* → `Clientes Interrumpidos` | Total que calcula el DMS. Se **suma por alimentador** (un caso trae una fila por alimentador) y se deduplica `(caso, alimentador)`. Es el número bueno. |
| | Bandeja capturada (columna de clientes) | Lo que muestre la grilla del DMS, si trae esa columna. |
| | NIS distintos con reclamo | **Mínimo**, no total: sólo los que llamaron. El mensaje lo dice explícitamente (`≥ 5 clientes (con reclamo…)`). |
| **Calles** | Dirección de reclamos / tareas del caso | Calles de los clientes **que reclamaron**: una muestra del área afectada, no el universo. Se muestran **sin número de casa**. |
| | Maestro de calles por SED (opcional) | Calles de todos los clientes de la SED afectada. |
| | Maestro de calles por alimentador (opcional) | Sólo se usa si la falla afecta ≥ 80 % de los clientes del alimentador (si no, sobrestimaría una falla parcial). |

**Límite que hay que decir:** ningún reporte conocido de Centrality entrega la
lista completa de clientes afectados con su dirección (el monitor
meteo-operacional llegó a la misma conclusión con los tres reportes del RPA).
Para tener *todas* las calles hace falta el maestro de calles por SED (§5),
que es un entregable de datos, no algo que el bot pueda inventar.

## 2. Fuentes que lee la extensión

| Fuente | Cómo | Estado |
|---|---|---|
| `abiertas` | Reporte **432 Tareas abiertas** (`param_1` = OU). El caso sale del código de tarea `########-D#/-SD#/-R#`. | ID verificado en el motor de despacho. |
| `casos` | *Informe de casos en periodos* (el que hoy baja el RPA a SharePoint SP2). | **ID desconocido**: usar «Descubrir reportes» en la extensión. |
| `reclamos` | Bandeja de reclamos del DMS como reporte. | **ID desconocido**: idem. Acepta encabezados por nombre o las 21 columnas posicionales del export `reclamos.saesa@…`. |
| `grilla` | Lee la(s) grilla(s) ExtJS **visibles** en la pestaña de Centrality (p. ej. la bandeja de casos/reclamos). No necesita ID de reporte. | Funciona con la bandeja abierta. **Nunca** se usa para dar una falla por normalizada (la grilla puede estar filtrada o paginada). |

El bot reconoce las columnas por **nombre** (con o sin tildes, mayúsculas,
dobles espacios) y en `/estado` muestra qué columnas reconoció y cuáles faltan
en cada fuente: es lo primero que hay que mirar al configurar un reporte nuevo.

## 3. Puesta en marcha

### 3.1 Bot local

Requisito: Python 3.10 o superior. **No necesita instalar nada con pip.**

```bat
cd bot-fallas-centrality
copy config.example.json config.json
REM editar config.json (ver 3.3) y luego:
python -m bot_fallas --config config.json --probar     REM envía un mensaje de prueba
iniciar_bot.bat                                         REM deja el bot corriendo
```

- Escucha **sólo** en `127.0.0.1:8771`. Estado en <http://127.0.0.1:8771/>.
- Para que arranque con Windows: copiar un acceso directo a `iniciar_bot.vbs`
  en `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup`.
- Estado y avisos en `datos/` (`estado.json`, `avisos.jsonl`, `bot.log`).

### 3.2 Extensión

1. Chrome → `chrome://extensions` → **Modo de desarrollador** → **Cargar descomprimida** → carpeta `extension/`.
2. **Opciones** de la extensión: dirección base de Centrality (la misma que
   usas en el navegador, p. ej. `https://<host>:<puerto>`) → **Guardar** →
   Chrome pide permiso para ese sitio: aceptar.
3. Abrir Centrality e iniciar sesión. **Debe quedar una pestaña de Centrality abierta.**
4. Icono de la extensión → **Diagnóstico** (los pasos deben quedar en ✅).
5. **Descubrir reportes** → anotar los ID del *informe de casos* y de la
   bandeja de *reclamos*, pegarlos en la tabla de fuentes y activarlas.
   `abiertas` (432) necesita el **ID de la OU**: el catálogo de OUs también
   sale en «Descubrir». Usar la OU del CCD y no la raíz del grupo: con la raíz
   el 432 tarda ~75 s.

> El host de Centrality **no está escrito en ningún archivo del repo** (el
> repositorio es público). Lo ingresa cada usuario en Opciones.

### 3.3 Telegram

1. Crear el bot con **@BotFather** → copiar el token.
2. Guardar el token en una variable de entorno (recomendado) en vez de en el archivo:
   `setx BOT_FALLAS_TELEGRAM_TOKEN "<token>"` (abrir una consola nueva después).
3. Agregar el bot al grupo de despacho, escribir `/id` en el grupo y copiar el
   `chat_id` (negativo para grupos) en `telegram.chats`.
4. `telegram.activo: true` y probar con `--probar`.

Comandos (sólo responden con datos en los chats autorizados):

| Comando | Respuesta |
|---|---|
| `/fallas` | Fallas activas, de mayor a menor cantidad de clientes |
| `/caso 40808515` | Detalle del caso, con hasta 40 calles |
| `/estado` | Edad de cada fuente, extensión, canales |
| `/id` | El `chat_id` del chat (para configurarlo) |

Teams (opcional): webhook de un flujo de **Workflows/Power Automate** en
`teams.webhook_url` o en la variable `BOT_FALLAS_TEAMS_WEBHOOK`. Envía una
*Adaptive Card*; no se ha probado contra el tenant de SAESA.

## 4. Cuándo avisa (y cuándo no)

| Evento | Regla por defecto (`config.json → avisos`) |
|---|---|
| **Nueva** | Caso abierto que pasa los filtros: ≥ 1 cliente según el DMS, o ≥ 3 clientes con reclamo si el DMS aún no informa total. Programadas excluidas. |
| **Actualizada** | Clientes suben ≥ 50 **y** ≥ 25 %, sube de nivel, aparecen ≥ 3 calles nuevas o se marca peligro. Máximo una cada 10 min por caso (salvo paso a MASIVO o peligro: inmediato). Un aviso frenado por el intervalo **no se pierde**: sale en la siguiente evaluación. |
| **Reposición parcial** | Clientes bajan ≥ 50 y ≥ 25 % sin llegar a cero. |
| **Normalizada** | Cierre explícito en el informe de casos, **o** el caso deja de aparecer en una fuente completa durante ≥ 15 min y ≥ 2 evaluaciones. El mensaje dice cuál de las dos fue. |
| **Al iniciar** | Un solo resumen de lo que ya estaba activo; no re-notifica caso por caso. |
| **Salud** | Aviso único si no llegan datos de Centrality en 30 min, si la sesión caducó, y cuando vuelven los datos. |

Niveles por clientes (los mismos cortes del Monitor de Afectaciones):
🟣 masivo ≥ 1.000 · 🔴 alta ≥ 300 · 🟠 media ≥ 50 · 🟡 baja · ⚪ sin dato.

Dos protecciones contra **falsas normalizaciones**, que son peores que un aviso de más:

1. Una fuente vieja (sin actualizar en `frescura_min`) no cuenta: si la
   extensión se cae, las fallas quedan como estaban, no se «normalizan».
2. Si el informe de casos es por ventana de fechas, un caso iniciado antes de
   esa ventana no se da por cerrado sólo porque salió de ella.

Filtros (`filtros`): `ccd`, `zonales`, `comunas`, `empresas`. Para saber CCD y
zonal de una falla hacen falta los maestros (§5); sin ellos, una falla de
zonal desconocida **se avisa** (`incluir_si_desconocido: true`).

## 5. Maestros opcionales (archivos locales, NO van al repo)

| Clave en `config.json → maestros` | Formato | Para qué |
|---|---|---|
| `alimentadores` | `dim_alimentador_cluster.csv` del monitor meteo-operacional (`COD, cod_centrality, …, NOMBRE ALIMENTADOR, clientes_total, comuna_dominante, …, ccd`) | Nombre del alimentador y subestación, comuna, filtro CCD Sur, ratio de alimentador completo. |
| `zonal` | `zonal_despacho_maestro.json` del monitor meteo-operacional | Zonal de despacho y CCD por comuna. |
| `calles` | CSV propio: `tipo,clave,calle,comuna,clientes` con `tipo` = `sed` o `alimentador` | Todas las calles de la SED / alimentador afectado. |

Se dejan en `datos/` (ignorado por git) y se apuntan con ruta relativa.

## 6. Privacidad y seguridad

- **Nunca sale del equipo**: NIS, nombre, teléfono, número de casa,
  comentarios, analistas. Los NIS sólo se usan en memoria para contar clientes
  distintos; `estado.json` guarda calles sin número y conteos.
- El servicio local acepta escrituras sólo desde la pestaña de Centrality
  configurada y desde extensiones de Chrome; cualquier otra página web que
  intente un POST recibe 403 (si no, cualquier sitio podría inventar fallas y
  mandarlas al grupo). Opcional: `servidor.token_compartido` + el mismo valor
  en la extensión.
- Telegram es un servicio externo: los avisos (calles y conteos) salen de la
  red de SAESA. Validar con TI/Compliance si corresponde usar Teams en su lugar.

## 7. Probar sin Centrality

```bat
python -m bot_fallas --config config.json --simular casos tests\fixtures\casos.csv
python -m unittest discover -s tests -v
node --test tests/js
```

`--simular` no envía nada (salvo `--enviar`) y no toca el estado real.

## 8. Pendientes conocidos

- Confirmar los **ID** del informe de casos y de la bandeja de reclamos con «Descubrir reportes».
- Confirmar qué columna de la bandeja trae la cantidad de clientes (las columnas de íconos 📞 ⚠️ ⚡ 👥 siguen sin significado documentado).
- Formato real de `Jerarquía de red` (de ahí sale la SED para el maestro de calles; hoy se extrae con una regex *best-effort*).
- Maestro de calles por SED: hay que conseguirlo (SAP/GIS).
- Chrome está desplegando restricciones de acceso a la red local: si una
  versión nueva bloquea el POST desde la pestaña, la extensión reintenta el
  envío desde el *service worker*.
