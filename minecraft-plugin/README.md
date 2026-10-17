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
