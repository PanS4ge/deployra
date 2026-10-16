# Deployra Relay (Port Bounce & Edge Routing)

Lekki kontener proxy (`nginx:alpine` w trybie `network_mode: host`) stworzony specjalnie dla środowisk z ograniczoną liczbą portów (np. Mikr.us, współdzielone VPS-y, maszyny za NAT-em lub z routingiem IPv6 przez Cloudflare).

## 🚀 Jak to działa?

1. **Dual-Stack (IPv4 + IPv6):**
   - Słucha jednocześnie na `0.0.0.0:80` oraz `[::]:80`.
   - Pozwala Cloudflare przekazywać ruch do maszyny bez żadnych publicznych adresów IPv4.

2. **Automatyczny Port Bounce (Zero Config):**
   - Pozwala dostać się do dowolnego portu na maszynie bez konfigurowania reguł!
   - Obsługuje prefiksy:
     - `npmpassthrough<id>.<port>` (pełna kompatybilność z Nginx Proxy Manager)
     - `<port>.domena` (np. `8080.frog01.pansage.xyz`)
     - `p<port>.domena` (np. `p8080.frog01.pansage.xyz`)
   - Zapytanie natychmiast trafia do `127.0.0.1:<port>`.

3. **Dedykowany routing do backendu Deployry:**
   - Zapytania do domeny węzła (np. `frog01.pansage.xyz`) oraz żądania z nagłówkiem `X-Deployra-Signature` są natychmiast kierowane do agenta Deployry (`127.0.0.1:20379` lub `30913`).

4. **Wydajność:**
   - Zużywa zaledwie **~2-4 MB RAM-u** (w przeciwieństwie do ciężkich procesów Pythona).
   - Pełna obsługa WebSockets, streamingu i długich połączeń.
