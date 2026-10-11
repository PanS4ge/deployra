# Plugins

*[Polska wersja](plugins.pl.md)*

A plugin is a directory with a `plugin.json` file. It can:

- add **app templates** to the wizard (“From a template”): no code needed, just JSON;
- add **language packs**, including a language the panel doesn't ship with;
- run **Python code** that adds pages with menu entries, reacts to events and stores its own settings.

Plugins live in `<DEPLOYRA_DATA>/plugins/<id>/` (`/var/lib/deployra-panel/plugins` in the panel container; override with
`DEPLOYRA_PLUGINS_DIR`). Install one by uploading a ZIP in **Settings → Plugins**, or copy the directory there and click
**Rescan**. Built-in plugins (the *App catalog*) can be disabled but not removed.

> Plugin code runs inside the panel process with the panel's full privileges: database, keys, nodes. An uploaded plugin stays
> disabled until someone enables it, and only users with the *risky settings* permission can upload, remove or enable plugins
> that contain code. Install plugins only from authors you trust.

## Layout

```
my-plugin/
├── plugin.json          required
├── plugin.py            optional — code (the "entry" file)
├── locales/en.json      optional — texts, also new languages (de.json…)
├── templates/page.html  optional — Jinja templates, available as "plugins/my-plugin/page.html"
└── static/logo.svg      optional — served at /plugins/my-plugin/static/logo.svg (logged-in users only)
```

A ready example with code, a page, settings and translations: [`examples/plugins/events-log`](../examples/plugins/events-log).
To install it, zip the directory (`events-log/plugin.json` in the archive is fine) and upload it in the panel.

## plugin.json

```json
{
  "id": "my-plugin",
  "name": {"pl": "Mój plugin", "en": "My plugin"},
  "version": "1.0.0",
  "author": "Me",
  "description": {"en": "What it does"},
  "requires": "1.0.0",
  "entry": "plugin.py",
  "app_templates": []
}
```

| Field | |
|---|---|
| `id` | `a-z`, `0-9`, `-`, `_`, 2–40 characters; must match the directory name |
| `name`, `description` | a string or `{"pl": …, "en": …}` |
| `requires` | the lowest Deployra version the plugin works with |
| `entry` | a `.py` file in the plugin directory with a `setup(api)` function |
| `app_templates` | wizard templates, see below |

## App templates

```json
{
  "id": "vaultwarden",
  "name": "Vaultwarden",
  "description": {"en": "Bitwarden-compatible password server"},
  "icon": "key",
  "image": "vaultwarden/server:latest",
  "container_port": 80,
  "env": {"SIGNUPS_ALLOWED": "false", "ADMIN_TOKEN": "${RANDOM}"},
  "volumes": ["vaultwarden:/data"],
  "memory_mb": 128
}
```

- `icon` is a [Bootstrap Icons](https://icons.getbootstrap.com/) name.
- `kind: "compose"` together with `compose` (YAML) and `expose_service` creates a compose app. The `env` values go to the stack's `.env`,
  so the YAML can use `${DB_PASSWORD}` (see WordPress in [`plugins_builtin/apps-catalog`](../frontend/deployra_panel/plugins_builtin/apps-catalog/plugin.json)).
- Every `${RANDOM}` in `env` and `compose` becomes a new random secret each time the wizard opens.
- A template only fills in the wizard: the user still picks the node and domain and can change everything before deploying.

## Code

```python
from fastapi import Request


def setup(api):
    router = api.router()                       # routes under /plugins/<id>, login required
    api.add_nav("title", "/plugins/my-plugin", "stars")

    async def on_deploy(event, data):
        api.logger.info(f"{data['app']['name']} is live")

    api.on("app.deployed", on_deploy)

    @router.get("")
    def page(request: Request):
        return api.render(request, "page.html", title=api.t("title"))
```

| `api.` | |
|---|---|
| `router()` | a FastAPI `APIRouter` with the prefix `/plugins/<id>`. Requires a logged-in user; returns 404 while the plugin is disabled |
| `add_nav(label, href, icon)` | a menu entry; `label` is a key from the plugin's pack (`"title"` → `plugin.<id>.title`), plain text or `{"pl": …, "en": …}` |
| `on(event, handler)` | `handler(event, data)`, sync or `async`; runs in the background, errors only go to the log |
| `add_app_template(dict)` | a wizard template created in code |
| `setting(key, default)` / `set_setting(key, value)` | the plugin's own settings in the panel database (JSON values) |
| `t(key, **params)` | text from the plugin's pack: `api.t("title")` = `t("plugin.<id>.title")` |
| `render(request, template, **context)` | renders `templates/<template>`; templates can `{% extends "base.html" %}` |
| `notify(message, level)` | a panel notification (and the `notification` event) |
| `panel`, `logger`, `dir`, `id` | the panel object (`panel.db`, `panel.config`…), a logger, the plugin directory, its id |

POST forms in plugin templates work like the rest of the panel: use `hx-post` (htmx sends the CSRF token by itself).

### Events

| Event | `data` |
|---|---|
| `app.deployed` | `app` (database row), `job` |
| `app.failed` | `app`, `job`, `error` |
| `app.removed` | `app` |
| `monitor.down` / `monitor.up` | `monitor`, `message` |
| `node.paired` | `node` (without the secret) |
| `update.available` | `version`, `current`, `release` |
| `notification` | `message`, `level` — every panel notification |
| `*` | all of the above |

The `app` row contains the app spec with its environment variables and the Git webhook secret. Don't send it anywhere as is
(the example plugin sends only the name, slug and status).

### Enabling and disabling

Disabling takes effect immediately: pages return 404, menu entries, templates and event handlers disappear. Python can't fully
unload a module, so after **updating** the code of an enabled plugin (a new ZIP) restart the panel. If `setup()` fails, the plugin
stays disabled and the error shows in Settings → Plugins; fix it and enable it again.

## Languages

Files in `locales/` are merged with the panel packs. Put your own texts under `plugin.<id>.…`:

```json
{"plugin": {"my-plugin": {"title": "My plugin"}}}
```

A pack for a language the panel doesn't have (e.g. `locales/de.json` with `"_language": "Deutsch"`) adds that language to the switcher.
Keys missing from it fall back to English. Start from `frontend/deployra_panel/locales/en.json` and
`shared/deployra_common/locales/en.json`; a language plugin doesn't need any code.
