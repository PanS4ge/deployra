# Pluginy

*[English version](plugins.md)*

Plugin to katalog z plikiem `plugin.json`. Może:

- dodać **szablony aplikacji** do kreatora („Z szablonu”) — bez kodu, sam JSON;
- dodać **paczki językowe**, także języka, którego panel nie ma;
- uruchomić **kod Pythona**: strony z pozycją w menu, reakcje na zdarzenia, własne ustawienia.

Pluginy leżą w `<DEPLOYRA_DATA>/plugins/<id>/` (w kontenerze panelu `/var/lib/deployra-panel/plugins`; zmienisz to przez
`DEPLOYRA_PLUGINS_DIR`). Instalacja: wgraj ZIP w **Ustawienia → Pluginy** albo skopiuj katalog i kliknij **Odśwież**.
Wbudowane pluginy (*Katalog aplikacji*) można wyłączyć, ale nie usunąć.

> Kod pluginu działa w procesie panelu z jego pełnymi uprawnieniami: baza, klucze, węzły. Wgrany plugin jest wyłączony, dopóki
> ktoś go nie włączy, a wgrać, usunąć albo włączyć plugin z kodem może tylko użytkownik z uprawnieniem do *ryzykownych ustawień*.
> Instaluj pluginy tylko od autorów, którym ufasz.

## Układ

```
moj-plugin/
├── plugin.json          wymagany
├── plugin.py            opcjonalny — kod (plik "entry")
├── locales/pl.json      opcjonalne — teksty, także nowe języki (de.json…)
├── templates/page.html  opcjonalne — szablony Jinja, dostępne jako "plugins/moj-plugin/page.html"
└── static/logo.svg      opcjonalne — pod /plugins/moj-plugin/static/logo.svg (dla zalogowanych)
```

Gotowy przykład z kodem, stroną, ustawieniami i tłumaczeniami: [`examples/plugins/events-log`](../examples/plugins/events-log).
Żeby go zainstalować, spakuj katalog do ZIP (może być `events-log/plugin.json` w archiwum) i wgraj w panelu.

## plugin.json

```json
{
  "id": "moj-plugin",
  "name": {"pl": "Mój plugin", "en": "My plugin"},
  "version": "1.0.0",
  "author": "Ja",
  "description": {"pl": "Co robi"},
  "requires": "1.0.0",
  "entry": "plugin.py",
  "app_templates": []
}
```

| Pole | |
|---|---|
| `id` | `a-z`, `0-9`, `-`, `_`, 2–40 znaków; musi być nazwą katalogu |
| `name`, `description` | napis albo `{"pl": …, "en": …}` |
| `requires` | najniższa wersja Deployry, z którą plugin działa |
| `entry` | plik `.py` w katalogu pluginu z funkcją `setup(api)` |
| `app_templates` | szablony do kreatora, niżej |

## Szablony aplikacji

```json
{
  "id": "vaultwarden",
  "name": "Vaultwarden",
  "description": {"pl": "Serwer haseł zgodny z Bitwarden"},
  "icon": "key",
  "image": "vaultwarden/server:latest",
  "container_port": 80,
  "env": {"SIGNUPS_ALLOWED": "false", "ADMIN_TOKEN": "${RANDOM}"},
  "volumes": ["vaultwarden:/data"],
  "memory_mb": 128
}
```

- `icon` to nazwa z [Bootstrap Icons](https://icons.getbootstrap.com/).
- `kind: "compose"` razem z `compose` (YAML) i `expose_service` tworzy aplikację compose. Wartości z `env` trafiają do `.env` stacka,
  więc YAML może używać `${DB_PASSWORD}` (zobacz WordPress w [`plugins_builtin/apps-catalog`](../frontend/deployra_panel/plugins_builtin/apps-catalog/plugin.json)).
- Każde `${RANDOM}` w `env` i `compose` zamienia się na nowy losowy sekret przy każdym otwarciu kreatora.
- Szablon tylko wypełnia kreator: węzeł i domenę użytkownik wybiera sam i przed wdrożeniem może wszystko zmienić.

## Kod

```python
from fastapi import Request


def setup(api):
    router = api.router()                       # trasy pod /plugins/<id>, wymagają zalogowania
    api.add_nav("title", "/plugins/moj-plugin", "stars")

    async def on_deploy(event, data):
        api.logger.info(f"{data['app']['name']} działa")

    api.on("app.deployed", on_deploy)

    @router.get("")
    def page(request: Request):
        return api.render(request, "page.html", title=api.t("title"))
```

| `api.` | |
|---|---|
| `router()` | `APIRouter` FastAPI z prefiksem `/plugins/<id>`. Wymaga zalogowania; gdy plugin jest wyłączony, zwraca 404 |
| `add_nav(label, href, icon)` | pozycja w menu; `label` to klucz z paczki pluginu (`"title"` → `plugin.<id>.title`), zwykły tekst albo `{"pl": …, "en": …}` |
| `on(event, handler)` | `handler(event, data)`, zwykła albo `async`; działa w tle, błędy idą tylko do logu |
| `add_app_template(dict)` | szablon do kreatora tworzony w kodzie |
| `setting(key, default)` / `set_setting(key, value)` | własne ustawienia pluginu w bazie panelu (wartości JSON) |
| `t(key, **params)` | tekst z paczki pluginu: `api.t("title")` = `t("plugin.<id>.title")` |
| `render(request, template, **context)` | renderuje `templates/<template>`; szablony mogą robić `{% extends "base.html" %}` |
| `notify(message, level)` | powiadomienie panelu (i zdarzenie `notification`) |
| `panel`, `logger`, `dir`, `id` | obiekt panelu (`panel.db`, `panel.config`…), logger, katalog pluginu, jego id |

Formularze POST w szablonach pluginu działają jak reszta panelu: używaj `hx-post` (htmx sam dokleja token CSRF).

### Zdarzenia

| Zdarzenie | `data` |
|---|---|
| `app.deployed` | `app` (wiersz z bazy), `job` |
| `app.failed` | `app`, `job`, `error` |
| `app.removed` | `app` |
| `monitor.down` / `monitor.up` | `monitor`, `message` |
| `node.paired` | `node` (bez sekretu) |
| `update.available` | `version`, `current`, `release` |
| `vulns.scanned` | `findings` (podatne paczki), `new` (nowo znalezione w tym skanie) |
| `notification` | `message`, `level` — każde powiadomienie panelu |
| `*` | wszystkie powyższe |

Wiersz `app` zawiera specyfikację aplikacji z jej zmiennymi środowiskowymi i sekret webhooka Gita. Nie wysyłaj go nigdzie w całości
(przykładowy plugin wysyła tylko nazwę, slug i status).

### Włączanie i wyłączanie

Wyłączenie działa od razu: strony zwracają 404, znikają pozycje w menu, szablony i obsługa zdarzeń. Pythona nie da się w pełni
wyładować z pamięci, więc po **aktualizacji** kodu włączonego pluginu (nowy ZIP) zrestartuj panel. Gdy `setup()` rzuci błąd,
plugin zostaje wyłączony, a błąd widać w Ustawienia → Pluginy; po poprawce włącz go jeszcze raz.

## Języki

Pliki z `locales/` dokładają się do paczek panelu. Własne teksty trzymaj pod `plugin.<id>.…`:

```json
{"plugin": {"moj-plugin": {"title": "Mój plugin"}}}
```

Paczka języka, którego panel nie ma (np. `locales/de.json` z `"_language": "Deutsch"`), dodaje ten język do przełącznika.
Brakujące w niej klucze spadają na angielski. Zacznij od `frontend/deployra_panel/locales/en.json` i
`shared/deployra_common/locales/en.json`; plugin językowy nie potrzebuje żadnego kodu.
