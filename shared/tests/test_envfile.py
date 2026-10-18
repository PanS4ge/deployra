"""Plik `.env`: parser, zapis, zakresy zmiennych i kolejność łączenia warstw."""

import pytest

from deployra_common.envfile import EnvFileError, dump, merge, parse
from deployra_common.schemas import AppSpec


def test_parse_basic_comments_and_export():
    text = "# komentarz\n\nA=1\nexport B=two\n  C = trzy  \nD=\nE=a#b\nF=wartość # uwaga\n"
    env, scopes = parse(text)
    assert env == {"A": "1", "B": "two", "C": "trzy", "D": "", "E": "a#b", "F": "wartość"}
    assert scopes == {}


def test_parse_quotes_and_multiline_values():
    text = 'A="linia1\nlinia2"\nB="w \\"cudzysłowie\\" i \\\\ ukośnik"\nC=\'dosłownie \\n\'\nD="a\\nb"\nE="  spacje  "\n'
    env, _ = parse(text)
    assert env == {
        "A": "linia1\nlinia2",
        "B": 'w "cudzysłowie" i \\ ukośnik',
        "C": "dosłownie \\n",
        "D": "a\nb",
        "E": "  spacje  ",
    }


def test_parse_scope_markers_apply_to_next_variable_only():
    env, scopes = parse("# @build\nNPM_TOKEN=x\n# zwykły komentarz\nPORT=80\n# @both\nDB=y\n# @runtime\nZ=1\n")
    assert env == {"NPM_TOKEN": "x", "PORT": "80", "DB": "y", "Z": "1"}
    assert scopes == {"NPM_TOKEN": "build", "DB": "both"}


@pytest.mark.parametrize("text", ["BEZ_ZNAKU", "1ZLE=x", 'A="niedomknięty\nBEZ_KONCA', "# @secret\nA=1"])
def test_parse_rejects_bad_lines(text):
    with pytest.raises(EnvFileError):
        parse(text)


def test_dump_round_trips_tricky_values():
    env = {
        "PLAIN": "abc",
        "EMPTY": "",
        "MULTI": "linia1\nlinia2",
        "SPACES": "  x  ",
        "HASH": "a #b",
        "QUOTE": '"w cudzysłowie"',
        "BACKSLASH": "C:\\temp\\x",
        "EQ": "a=b",
    }
    scopes = {"HASH": "build", "MULTI": "both"}
    assert parse(dump(env, scopes)) == (env, scopes)


def test_merge_later_layers_override_and_keep_their_scope():
    group_a = ({"URL": "a", "TOKEN": "t1", "X": "1"}, {"TOKEN": "build"})
    group_b = ({"URL": "b", "Y": "2"}, {})
    app = ({"X": "app", "Z": "3"}, {"Z": "both"})
    env, scopes = merge([group_a, group_b, app])
    assert env == {"URL": "b", "TOKEN": "t1", "X": "app", "Y": "2", "Z": "3"}
    assert scopes == {"TOKEN": "build", "Z": "both"}  # X nadpisany przez aplikację (zakres runtime)
    assert merge([group_b, group_a]) == ({"URL": "a", "Y": "2", "TOKEN": "t1", "X": "1"}, {"TOKEN": "build"})
    assert merge([({"A": "1"}, {"A": "build"}), ({"A": "2"}, {})]) == ({"A": "2"}, {})


def test_app_spec_splits_runtime_and_build_env():
    spec = AppSpec.model_validate({
        "slug": "web",
        "source": {"kind": "git", "git_url": "https://example.com/r.git"},
        "container_port": 3000,
        "env": {"A": "1", "SECRET_BUILD": "b", "BOTH": "x"},
        "env_scopes": {"SECRET_BUILD": "build", "BOTH": "both", "NIEZNANA": "build", "A": "runtime"},
    })
    assert spec.env_scopes == {"SECRET_BUILD": "build", "BOTH": "both"}  # „runtime” i obce klucze znikają
    assert spec.runtime_env() == {"A": "1", "BOTH": "x"}
    assert spec.build_env() == {"SECRET_BUILD": "b", "BOTH": "x"}


def test_app_spec_without_scopes_keeps_everything_at_runtime():
    spec = AppSpec.model_validate({"slug": "old", "source": {"kind": "image", "image": "nginx"}, "container_port": 80, "env": {"A": "1"}})
    assert spec.env_scopes == {} and spec.runtime_env() == {"A": "1"} and spec.build_env() == {}
