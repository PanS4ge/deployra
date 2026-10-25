import re
from pathlib import Path

from deployra_panel.web import templates

TEMPLATES = Path(__file__).resolve().parent.parent / "deployra_panel" / "templates"
# `x.update` w Jinja na słowniku to metoda dict.update, nie pole „update” — tak tabela komponentów pokazywała „brak” wszędzie
DICT_METHOD = re.compile(r"(?<![\w\"'.$])([a-z_]\w*)\.(update|items|keys|values|pop|copy|clear|setdefault|get)\b(?!\s*\()")


def test_templates_do_not_read_dict_methods_as_fields():
    found = []
    for path in TEMPLATES.rglob("*.html"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = " ".join(re.findall(r"{[{%](.*?)[%}]}", line))  # tylko wyrażenia Jinja (bez tekstu i Alpine)
            for match in DICT_METHOD.finditer(code):
                found.append(f"{path.relative_to(TEMPLATES)}:{number}: {match.group(0)}")
    assert not found, found


def test_components_table_shows_helper_and_update_button():
    html = templates.env.get_template("partials/update_components.html").render(
        items=[{"key": "node-1", "name": "oracle1", "icon": "hdd-network", "online": True, "version": "1.6.0", "behind": True, "address": "10.66.0.3",
                "update": {"available": True, "mode": "helper", "state": "idle", "reason": "no_container"}}],
        target="1.7.3", pending=None, describe=lambda u: "", t=lambda key, **kw: key, user=None,
    )  # fmt: skip
    assert "updates.mode.helper" in html and "/settings/updates/start" in html and "updates.manual_hint" not in html
