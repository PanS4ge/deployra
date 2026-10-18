# DeployraBridge

Plugin Paper / Spigot / Purpur (1.8.8–1.21.x, bajtkod Java 8), dzięki któremu serwer Minecraft widać w panelu Deployry:
konsola z makrami, gracze (tryb gry, leczenie, OP, whitelist, kick, ban), `server.properties`, pliki i PlaceholderAPI.

## Bezpieczeństwo

- **Serwer nie otwiera żadnego portu.** Plugin sam łączy się z panelem po HTTPS i czeka na polecenia (długie zapytanie).
- **Parowanie jednorazowym kodem** (ważny 24 h). Panel wydaje w zamian losowy sekret, który zostaje w `plugins/DeployraBridge/config.yml`;
  w panelu jest zaszyfrowany kluczem panelu.
- **Podpisy HMAC-SHA256 w obie strony** (czas, jednorazowy nonce, skrót treści) — podrobione albo powtórzone polecenie zostanie odrzucone.
- Komendy idą jako konsola na głównym wątku serwera; pliki są ograniczone do katalogu serwera (bez wychodzenia przez `..` ani dowiązania).

## Instalacja na hostingu (Craftserve, Pterodactyl, …)

1. W panelu: **Minecraft → Dodaj serwer zewnętrzny**. Panel pokaże polecenie z kodem.
2. Wgraj `DeployraBridge.jar` (do pobrania w panelu) do `plugins/` i uruchom serwer.
3. W konsoli serwera wpisz polecenie z panelu: `deployra pair https://panel.example.com <kod>`.

`/deployra status` pokazuje stan połączenia, `/deployra unpair` usuwa sekret.

Pliki wyłączonego serwera: w zakładce **Połączenie** można podać FTP hostingu — panel użyje go, gdy plugin jest offline.

Duże pliki (od 1.2): `files.write_chunk` dopisuje kolejne kawałki (do 4 MB, base64) do ukrytego pliku `.<nazwa>.deployra-<id>.part`
obok celu i odrzuca kawałek spoza kolejności; `files.commit` sprawdza rozmiar i SHA-256, po czym atomowo podmienia plik; `files.abort`
usuwa niedokończony plik. `files.read_chunk` zwraca fragment pliku dowolnej wielkości. Plugin ogłasza to panelowi w `info.features`
(`files.chunks`), więc starszy plugin dalej działa z limitem 8 MB na plik.

Pluginy (od 1.2): `plugins.list` zwraca wczytane pluginy i jary wyłączone na stałe, `plugins.disable` wyłącza plugin od razu
(z `persist` — dodatkowo zmienia `X.jar` na `X.jar.disabled`, więc nie wczyta się po restarcie), `plugins.enable` cofa jedno i drugie.
DeployraBridge nie da się wyłączyć z panelu — to on łączy serwer z panelem.

Aktualizacja z panelu: nowy jar trafia do `plugins/update/` pod nazwą obecnego pliku pluginu, a Bukkit/Paper podmienia go przy starcie.

## Serwer jako aplikacja Deployry

**Aplikacje → Nowa → Minecraft (Paper)** stawia `itzg/minecraft-server` na węźle (port gry 25565). Obraz pobiera plugin z panelu
(`PLUGINS=`), a ten paruje się sam kodem ze zmiennej `DEPLOYRA_PAIR_CODE`. Wymaga silnika z pluginami (Paper, Spigot, Purpur) —
Vanilla, Fabric i Forge nie są obsługiwane.

## Budowanie

```bash
sh build.sh
```

Skrypt kompiluje `javac --release 8` względem jarów API w `libs/` (paper-api i jego zależności; nie są w repozytorium) i zapisuje
`frontend/deployra_panel/static/downloads/DeployraBridge.jar`. Alternatywnie `mvn package` (pom.xml pobiera paper-api z repo.papermc.io).
