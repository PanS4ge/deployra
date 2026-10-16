# DeployraBridge — Minecraft Remote Bridge

Lekki plugin (51 KB, **0 zewnętrznych zależności runtime**, bytecode Java 8) pozwalający kompleksowo zarządzać serwerem Minecraft z poziomu **Deployry** lub dowolnego zewnętrznego REST API na **współdzielonych hostingach** (Craftserve, TitanAxe, Pterodactyl, Falix itp.), gdzie nie masz dostępu do konta root ani gniazda Dockera.

Działa na **wszystkich wersjach Minecrafta od 1.8.8 aż po 1.21.x** (Paper, Spigot, Purpur, Tuinity).

---

## 🚀 Jak zainstalować na hostingu współdzielonym?

1. Wrzuć plik `DeployraBridge-1.0.0.jar` do folderu `plugins/` na swoim serwerze Minecraft.
2. Zrestartuj serwer Minecraft (lub wpisz `reload confirm`).
3. Plugin utworzy folder `plugins/DeployraBridge/` z plikiem `config.yml` i automatycznie wygeneruje unikalny token bezpieczeństwa.
4. Otwórz `plugins/DeployraBridge/config.yml`:
   ```yaml
   # Przypisz port przydzielony Ci przez hosting (np. port dodatkowy w panelu Pterodactyl/Craftserve)
   port: 25585
   host: "0.0.0.0"

   # Wygenerowany unikalny token bezpieczeństwa
   token: "dep_9a7b6c5d..."

   max-log-lines: 150
   enable-remote-commands: true
   ```
5. Przeładuj konfigurację w konsoli serwera: `/deployra reload`.

---

## 🎮 Zintegrowane funkcje w panelu Deployra

### 1. Widok gracza w stylu Aternos (Player Details)
* **Awatar 3D / głowa gracza** ładowana z sieci Minotar / mc-heads.
* **UUID gracza** z przyciskiem szybkiego kopiowania.
* **Wybór trybu gry (Gamemode)**: Survival, Creative, Adventure, Spectator.
* **Pasek serduszek (Health)**: interaktywny wskaźnik serduszek z przyciskami **Ulecz (Heal)** oraz **Zabij (Kill)**.
* **Pasek głodu (Hunger)**: wskaźnik poziomu sytości z przyciskami **Nakarm (Feed)** oraz **Wygłodź (Starve)**.
* **Przełączniki kontroli (Control toggles)**:
  * **Biała lista (Whitelisted)**
  * **Operator (OP)**
  * **Zbanowany (Banned)**
  * **Wyrzuć (Kick)**

### 2. Opcje serwera (server.properties)
* Wizualny edytor parametrów serwera:
  * Miejsca (Slots / max-players)
  * Tryb gry (Gamemode)
  * Trudność (Difficulty)
  * Biała lista (Whitelist)
  * Nieoryginalne (Cracked / online-mode)
  * Latanie (Allow Flight)
  * Wymuś tryb gry (Force Gamemode)
  * PVP, Command Blocks, Nether, Monsters, Animals
  * Ochrona spawnu (Spawn Protection)
  * Paczka zasobów (Resource Pack) oraz MOTD
* Zmiany zapisują się bezpośrednio przez klienta **FTP** lub mostek pluginu.

### 3. Zintegrowany Menedżer Plików (FTP Client)
* Wbudowany klient FTP / FTPS w panelu Deployry do przeglądania folderów, pobierania, edycji i usuwania plików.
* Fallback do bridge pluginu, jeśli port FTP jest zablokowany.
* Wbudowany edytor plików tekstowych (`.yml`, `.properties`, `.json`, `.log`).

### 4. Obsługa PlaceholderAPI (PAPI)
* Plugin automatycznie wykrywa obecność PlaceholderAPI na serwerze przez bezpieczną refleksję (brak twardej zależności).
* Inspektor placeholderów na żywo w Deployrze — wpisz dowolny placeholder (np. `%server_tps%`, `%player_ping%`, `%vault_eco_balance%`) i sprawdź jego bieżącą wartość.

### 5. Predefiniowane Makra (Quick Actions)
* Zestaw domyślnych makr (`Dzień`, `Noc`, `Czyste niebo`, `Zapisz świat`, `Przeładuj`, `Ulecz wszystkich`, `Nakarm wszystkich`, `Wyczyść przedmioty`, `Whitelist ON/OFF`, `TPS`).
* Możliwość definiowania własnych makr z komendą, ikoną i kolorem.

---

## 🔌 API Endpoints w `DeployraBridge`

Wszystkie żądania autoryzowane wymagają nagłówka `Authorization: Bearer <token>` lub `X-Deployra-Key: <token>`.

* `GET /health` — status i wersja serwera
* `GET /api/info` — metryki: TPS, RAM, liczba graczy, uptime
* `POST /api/command` — zdalne wykonanie komendy w konsoli z przechwyceniem odpowiedzi
* `GET /api/players` — lista graczy online z UUID, pozycją XYZ, pingiem, HP i głodem
* `POST /api/players/action` — manipulacja graczem (`heal`, `feed`, `kill`, `gamemode`, `op`, `whitelist`, `ban`, `pardon`, `kick`)
* `GET /api/properties` / `POST /api/properties` — odczyt i zapis `server.properties`
* `GET /api/files/list` / `GET /api/files/read` / `POST /api/files/write` / `POST /api/files/delete` / `POST /api/files/mkdir` — operacje na plikach
* `GET /api/papi/status` — sprawdzenie PlaceholderAPI i lista zarejestrowanych rozszerzeń
* `POST /api/papi/parse` — ewaluacja listy placeholderów
