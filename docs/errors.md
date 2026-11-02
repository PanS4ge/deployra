# Kody błędów (`⌁ DPL-…`)

Każde nieudane zadanie (wdrożenie, naprawa, aktualizacja, przeniesienie…) kończy się w logu dwiema liniami:

```
✖ port hosta 8080 jest zajęty (kontener errpage) — zmień „Port hosta” …
⌁ DPL-PORT · node 1.8.1 · deploy · ports.py:95 · app=errpage port=8080 holder=kontener errpage
```

Druga linia to „odcisk” błędu do zgłoszenia — wklej ją razem z kilkoma liniami nad nią. Części, oddzielone ` · `:

| Część | Znaczenie |
|---|---|
| `DPL-PORT` | kod błędu (tabela niżej) |
| `node 1.8.1` | który komponent zgłosił błąd (`panel`, `node`, `wg`) i w jakiej wersji |
| `deploy` | rodzaj zadania |
| `ports.py:95` | plik i linia w kodzie Deployry, gdzie błąd powstał |
| `← RemoteError@relay.py:44` | (opcjonalnie) błąd pierwotny, opakowany w czytelny komunikat |
| `app=… port=…` | kontekst: aplikacja (`slug#id`), węzeł, krok, port, kontener, obraz… |

Błąd z węzła przechodzi do panelu z tym samym kodem — w logu panelu są wtedy dwie linie `⌁` (wcięta z węzła i końcowa z panelu,
z nazwą aplikacji i węzła). Linia nigdy nie zawiera wartości zmiennych środowiskowych ani haseł.

## Spis kodów

| Kod | Co się stało | Zwykle pomaga |
|---|---|---|
| `DPL-PORT` | port hosta aplikacji trzyma inny kontener (`holder`) albo program | zmień „Port hosta” albo zostaw pusty; jeśli `holder` to stary kontener tej samej aplikacji spoza Deployry — „Napraw”/aktualizacja robi to teraz w miejscu |
| `DPL-PORT-POOL` | w puli portów węzła (`pool`) nie ma wolnego | poszerz `DEPLOYRA_PORT_POOL` na węźle albo usuń nieużywane aplikacje |
| `DPL-INPLACE-START` | aktualizacja w miejscu: nowy kontener (`image`) nie wstał / nie odpowiedział — wrócił stary (`previous`) | logi nad linią; zwykle niezgodność nowej wersji (np. PHP 8.2 vs stary kod) |
| `DPL-INPLACE-BACKUP` | działa kopia `<nazwa>-pre-update` z poprzedniej aktualizacji | zatrzymaj albo usuń kopię |
| `DPL-SYSTEM` | aplikacja wskazuje kontener samej Deployry | Deployrę aktualizuje aktualizator (Ustawienia → Aktualizacje), nie „Wdróż”/„Napraw” |
| `DPL-NODE-<status>` | API węzła odmówiło (`400` zły wniosek, `404` brak kontenera/trasy — często za stary węzeł, `409` konflikt) | komunikat obok; przy `404` zaktualizuj węzeł |
| `DPL-NODE-JOB` | zadanie na węźle padło bez własnego kodu (węzeł starszy niż 1.8.1) | linie z węzła (wcięte) nad końcem |
| `DPL-NET` | panel nie dogadał się z węzłem / bramką (sieć, tunel, timeout) | stan tunelu i węzła na stronie Węzły |
| `DPL-JOB` | czytelny błąd bez osobnego kodu — patrz plik i linia | komunikat nad linią |
| `DPL-BUG` | nieoczekiwany wyjątek (nad linią jest ślad stosu) — błąd w Deployrze | zgłoś z całym śladem |
