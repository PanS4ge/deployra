package io.deployra.minecraft;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpHandler;
import com.sun.net.httpserver.HttpServer;
import org.bukkit.Bukkit;
import org.bukkit.GameMode;
import org.bukkit.OfflinePlayer;
import org.bukkit.World;
import org.bukkit.entity.Player;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.lang.management.ManagementFactory;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;

public class DeployraHttpServer {

    private final DeployraPlugin plugin;
    private final String host;
    private final int port;
    private final String token;
    private final boolean enableRemoteCommands;
    private final LogCaptureHandler logCapture;

    private HttpServer server;
    private ExecutorService executor;

    public DeployraHttpServer(DeployraPlugin plugin, String host, int port, String token, boolean enableRemoteCommands, LogCaptureHandler logCapture) {
        this.plugin = plugin;
        this.host = host;
        this.port = port;
        this.token = token;
        this.enableRemoteCommands = enableRemoteCommands;
        this.logCapture = logCapture;
    }

    public void start() throws IOException {
        InetSocketAddress address = (host == null || host.isEmpty() || host.equals("0.0.0.0"))
                ? new InetSocketAddress(port)
                : new InetSocketAddress(host, port);

        server = HttpServer.create(address, 0);
        executor = Executors.newFixedThreadPool(6);
        server.setExecutor(executor);

        // Core Endpoints
        server.createContext("/", new RootHandler());
        server.createContext("/health", new HealthHandler());
        server.createContext("/api/info", new InfoHandler());
        server.createContext("/api/status", new InfoHandler());
        server.createContext("/api/command", new CommandHandler());
        server.createContext("/api/action", new ActionHandler());
        server.createContext("/api/logs", new LogsHandler());

        // Player Endpoints
        server.createContext("/api/players", new PlayersHandler());
        server.createContext("/api/players/action", new PlayerActionHandler());

        // Options / Properties Endpoints
        server.createContext("/api/properties", new PropertiesHandler());

        // Files Endpoints (Bridge Fallback / Integrated Manager)
        server.createContext("/api/files/list", new FilesListHandler());
        server.createContext("/api/files/read", new FilesReadHandler());
        server.createContext("/api/files/write", new FilesWriteHandler());
        server.createContext("/api/files/delete", new FilesDeleteHandler());
        server.createContext("/api/files/mkdir", new FilesMkdirHandler());

        // PlaceholderAPI Endpoints
        server.createContext("/api/papi/status", new PapiStatusHandler());
        server.createContext("/api/papi/parse", new PapiParseHandler());

        server.start();
        plugin.getLogger().info("Deployra HTTP Bridge listening on " + address.getHostString() + ":" + address.getPort());
    }

    public void stop() {
        if (server != null) {
            server.stop(1);
            server = null;
        }
        if (executor != null) {
            executor.shutdownNow();
            executor = null;
        }
    }

    private boolean isAuthorized(HttpExchange exchange) {
        if (token == null || token.trim().isEmpty()) {
            return true;
        }

        String authHeader = exchange.getRequestHeaders().getFirst("Authorization");
        if (authHeader != null) {
            if (authHeader.startsWith("Bearer ")) {
                authHeader = authHeader.substring(7).trim();
            }
            if (authHeader.equals(token)) return true;
        }

        String keyHeader = exchange.getRequestHeaders().getFirst("X-Deployra-Key");
        if (keyHeader == null) keyHeader = exchange.getRequestHeaders().getFirst("X-Deployra-Token");
        if (keyHeader != null && keyHeader.trim().equals(token)) return true;

        String query = exchange.getRequestURI().getQuery();
        if (query != null) {
            for (String param : query.split("&")) {
                String[] pair = param.split("=");
                if (pair.length == 2 && pair[0].equalsIgnoreCase("token") && pair[1].equals(token)) {
                    return true;
                }
            }
        }

        return false;
    }

    private void sendJson(HttpExchange exchange, int statusCode, Object data) throws IOException {
        byte[] bytes = JsonHelper.toJson(data).getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json; charset=utf-8");
        exchange.getResponseHeaders().set("Access-Control-Allow-Origin", "*");
        exchange.getResponseHeaders().set("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
        exchange.getResponseHeaders().set("Access-Control-Allow-Headers", "Authorization, X-Deployra-Key, X-Deployra-Token, Content-Type");
        exchange.sendResponseHeaders(statusCode, bytes.length);
        try (OutputStream os = exchange.getResponseBody()) {
            os.write(bytes);
        }
    }

    private String readBody(HttpExchange exchange) throws IOException {
        try (InputStream is = exchange.getRequestBody();
             ByteArrayOutputStream baos = new ByteArrayOutputStream()) {
            byte[] buf = new byte[2048];
            int n;
            while ((n = is.read(buf)) != -1) {
                baos.write(buf, 0, n);
            }
            return baos.toString(StandardCharsets.UTF_8.name());
        }
    }

    private Map<String, String> getQueryParams(HttpExchange exchange) {
        Map<String, String> map = new HashMap<>();
        String query = exchange.getRequestURI().getQuery();
        if (query == null) return map;
        for (String param : query.split("&")) {
            String[] pair = param.split("=");
            if (pair.length == 2) {
                try {
                    map.put(pair[0], java.net.URLDecoder.decode(pair[1], "UTF-8"));
                } catch (Exception ignored) {
                    map.put(pair[0], pair[1]);
                }
            }
        }
        return map;
    }

    private boolean handleCors(HttpExchange exchange) throws IOException {
        if ("OPTIONS".equalsIgnoreCase(exchange.getRequestMethod())) {
            exchange.getResponseHeaders().set("Access-Control-Allow-Origin", "*");
            exchange.getResponseHeaders().set("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
            exchange.getResponseHeaders().set("Access-Control-Allow-Headers", "Authorization, X-Deployra-Key, X-Deployra-Token, Content-Type");
            exchange.sendResponseHeaders(204, -1);
            return true;
        }
        return false;
    }

    // --- HANDLERS ---

    private class RootHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (handleCors(exchange)) return;
            Map<String, Object> res = new LinkedHashMap<>();
            res.put("ok", true);
            res.put("service", "DeployraMinecraftBridge");
            res.put("version", plugin.getDescription().getVersion());
            sendJson(exchange, 200, res);
        }
    }

    private class HealthHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (handleCors(exchange)) return;
            Map<String, Object> res = new LinkedHashMap<>();
            res.put("ok", true);
            res.put("service", "DeployraMinecraft");
            res.put("version", plugin.getDescription().getVersion());
            res.put("server", Bukkit.getVersion());
            res.put("status", "online");
            sendJson(exchange, 200, res);
        }
    }

    private class InfoHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (handleCors(exchange)) return;
            if (!isAuthorized(exchange)) {
                sendJson(exchange, 401, Collections.singletonMap("error", "Unauthorized: invalid or missing token"));
                return;
            }

            Runtime rt = Runtime.getRuntime();
            long maxRam = rt.maxMemory() / (1024 * 1024);
            long totalRam = rt.totalMemory() / (1024 * 1024);
            long freeRam = rt.freeMemory() / (1024 * 1024);
            long usedRam = totalRam - freeRam;

            Map<String, Object> ram = new LinkedHashMap<>();
            ram.put("used_mb", usedRam);
            ram.put("free_mb", freeRam);
            ram.put("total_mb", totalRam);
            ram.put("max_mb", maxRam);

            List<String> playerNames = new ArrayList<>();
            for (Player p : Bukkit.getOnlinePlayers()) {
                playerNames.add(p.getName());
            }

            double[] tps = getRecentTps();
            List<Double> tpsList = new ArrayList<>();
            for (double t : tps) {
                tpsList.add(Math.round(Math.min(20.0, t) * 100.0) / 100.0);
            }

            long uptimeSeconds = ManagementFactory.getRuntimeMXBean().getUptime() / 1000;

            Map<String, Object> res = new LinkedHashMap<>();
            res.put("ok", true);
            res.put("server_name", Bukkit.getServer().getName());
            res.put("version", Bukkit.getVersion());
            res.put("bukkit_version", Bukkit.getBukkitVersion());
            res.put("motd", Bukkit.getMotd());
            res.put("players_online", Bukkit.getOnlinePlayers().size());
            res.put("players_max", Bukkit.getMaxPlayers());
            res.put("players", playerNames);
            res.put("tps", tpsList);
            res.put("ram", ram);
            res.put("uptime_seconds", uptimeSeconds);
            res.put("whitelist_enabled", Bukkit.hasWhitelist());
            res.put("papi_enabled", PlaceholderApiBridge.isAvailable());

            sendJson(exchange, 200, res);
        }
    }

    private class CommandHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (handleCors(exchange)) return;
            if (!isAuthorized(exchange)) {
                sendJson(exchange, 401, Collections.singletonMap("error", "Unauthorized"));
                return;
            }
            if (!enableRemoteCommands) {
                sendJson(exchange, 403, Collections.singletonMap("error", "Remote commands are disabled in config.yml"));
                return;
            }
            if (!"POST".equalsIgnoreCase(exchange.getRequestMethod())) {
                sendJson(exchange, 405, Collections.singletonMap("error", "Method Not Allowed"));
                return;
            }

            String body = readBody(exchange);
            String command = JsonHelper.extractString(body, "command");
            if (command == null || command.isEmpty()) {
                command = JsonHelper.extractString(body, "cmd");
            }

            if (command == null || command.trim().isEmpty()) {
                sendJson(exchange, 400, Collections.singletonMap("error", "Missing 'command' parameter"));
                return;
            }

            final String cmd = command.startsWith("/") ? command.substring(1) : command;
            final CompletableFuture<String> future = new CompletableFuture<>();

            Bukkit.getScheduler().runTask(plugin, () -> {
                CommandCaptureSender captureSender = new CommandCaptureSender();
                try {
                    boolean success = Bukkit.dispatchCommand(captureSender.getSender(), cmd);
                    String output = captureSender.getOutputJoined();
                    if (output.isEmpty()) {
                        output = success ? "Command executed successfully (no output)." : "Command failed or unknown command.";
                    }
                    future.complete(output);
                } catch (Exception e) {
                    future.complete("Exception executing command: " + e.getMessage());
                }
            });

            try {
                String resultOutput = future.get(8, TimeUnit.SECONDS);
                Map<String, Object> res = new LinkedHashMap<>();
                res.put("ok", true);
                res.put("command", cmd);
                res.put("output", resultOutput);
                sendJson(exchange, 200, res);
            } catch (Exception e) {
                sendJson(exchange, 500, Collections.singletonMap("error", "Command timed out or error: " + e.getMessage()));
            }
        }
    }

    private class PlayersHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (handleCors(exchange)) return;
            if (!isAuthorized(exchange)) {
                sendJson(exchange, 401, Collections.singletonMap("error", "Unauthorized"));
                return;
            }

            List<Map<String, Object>> players = new ArrayList<>();
            for (Player p : Bukkit.getOnlinePlayers()) {
                Map<String, Object> pInfo = new LinkedHashMap<>();
                pInfo.put("name", p.getName());
                pInfo.put("uuid", p.getUniqueId().toString());
                pInfo.put("health", Math.round(p.getHealth() * 10.0) / 10.0);
                pInfo.put("max_health", Math.round(p.getMaxHealth() * 10.0) / 10.0);
                pInfo.put("food", p.getFoodLevel());
                pInfo.put("gamemode", p.getGameMode().name());
                pInfo.put("op", p.isOp());
                pInfo.put("whitelisted", p.isWhitelisted());
                pInfo.put("ping", getPlayerPing(p));
                pInfo.put("world", p.getWorld().getName());
                pInfo.put("x", Math.round(p.getLocation().getX() * 10.0) / 10.0);
                pInfo.put("y", Math.round(p.getLocation().getY() * 10.0) / 10.0);
                pInfo.put("z", Math.round(p.getLocation().getZ() * 10.0) / 10.0);
                players.add(pInfo);
            }

            Map<String, Object> res = new LinkedHashMap<>();
            res.put("ok", true);
            res.put("count", players.size());
            res.put("players", players);
            sendJson(exchange, 200, res);
        }
    }

    private class PlayerActionHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (handleCors(exchange)) return;
            if (!isAuthorized(exchange)) {
                sendJson(exchange, 401, Collections.singletonMap("error", "Unauthorized"));
                return;
            }
            if (!"POST".equalsIgnoreCase(exchange.getRequestMethod())) {
                sendJson(exchange, 405, Collections.singletonMap("error", "Method Not Allowed"));
                return;
            }

            String body = readBody(exchange);
            String playerName = JsonHelper.extractString(body, "player");
            String action = JsonHelper.extractString(body, "action");
            String value = JsonHelper.extractString(body, "value");

            if (playerName == null || action == null) {
                sendJson(exchange, 400, Collections.singletonMap("error", "Missing 'player' or 'action' parameter"));
                return;
            }

            final CompletableFuture<Map<String, Object>> future = new CompletableFuture<>();

            Bukkit.getScheduler().runTask(plugin, () -> {
                Map<String, Object> out = new LinkedHashMap<>();
                Player player = Bukkit.getPlayerExact(playerName);
                OfflinePlayer offlinePlayer = Bukkit.getOfflinePlayer(playerName);

                try {
                    switch (action.toLowerCase()) {
                        case "heal":
                            if (player != null) {
                                player.setHealth(player.getMaxHealth());
                                player.setFoodLevel(20);
                                out.put("ok", true);
                                out.put("message", "Gracz " + playerName + " został uleczony.");
                            } else {
                                out.put("ok", false);
                                out.put("error", "Gracz jest offline.");
                            }
                            break;

                        case "feed":
                            if (player != null) {
                                player.setFoodLevel(20);
                                out.put("ok", true);
                                out.put("message", "Gracz " + playerName + " został nakarmiony.");
                            } else {
                                out.put("ok", false);
                                out.put("error", "Gracz jest offline.");
                            }
                            break;

                        case "starve":
                            if (player != null) {
                                player.setFoodLevel(0);
                                out.put("ok", true);
                                out.put("message", "Gracz " + playerName + " został wygłodzony.");
                            } else {
                                out.put("ok", false);
                                out.put("error", "Gracz jest offline.");
                            }
                            break;

                        case "kill":
                            if (player != null) {
                                player.setHealth(0.0);
                                out.put("ok", true);
                                out.put("message", "Gracz " + playerName + " został zabity.");
                            } else {
                                out.put("ok", false);
                                out.put("error", "Gracz jest offline.");
                            }
                            break;

                        case "gamemode":
                            if (player != null && value != null) {
                                GameMode gm = GameMode.valueOf(value.toUpperCase());
                                player.setGameMode(gm);
                                out.put("ok", true);
                                out.put("gamemode", gm.name());
                                out.put("message", "Zmieniono tryb gry gracza " + playerName + " na " + gm.name());
                            } else {
                                out.put("ok", false);
                                out.put("error", "Gracz jest offline lub nieprawidłowy tryb gry.");
                            }
                            break;

                        case "op":
                            boolean opState = value == null || !value.equalsIgnoreCase("false");
                            if (player != null) player.setOp(opState);
                            else offlinePlayer.setOp(opState);
                            out.put("ok", true);
                            out.put("op", opState);
                            out.put("message", "Zmieniono uprawnienia OP dla " + playerName);
                            break;

                        case "whitelist":
                            boolean wlState = value == null || !value.equalsIgnoreCase("false");
                            offlinePlayer.setWhitelisted(wlState);
                            out.put("ok", true);
                            out.put("whitelisted", wlState);
                            out.put("message", "Zmieniono status whitelist dla " + playerName);
                            break;

                        case "ban":
                            String reason = (value != null && !value.isEmpty()) ? value : "Zbanowany z poziomu Deployry";
                            Bukkit.dispatchCommand(Bukkit.getConsoleSender(), "ban " + playerName + " " + reason);
                            out.put("ok", true);
                            out.put("banned", true);
                            out.put("message", "Gracz " + playerName + " został zbanowany.");
                            break;

                        case "pardon":
                            Bukkit.dispatchCommand(Bukkit.getConsoleSender(), "pardon " + playerName);
                            out.put("ok", true);
                            out.put("banned", false);
                            out.put("message", "Gracz " + playerName + " został odbanowany.");
                            break;

                        case "kick":
                            String kickReason = (value != null && !value.isEmpty()) ? value : "Wyrzucony przez administratora";
                            if (player != null) {
                                player.kickPlayer(kickReason);
                                out.put("ok", true);
                                out.put("message", "Gracz " + playerName + " został wyrzucony.");
                            } else {
                                out.put("ok", false);
                                out.put("error", "Gracz jest offline.");
                            }
                            break;

                        default:
                            out.put("ok", false);
                            out.put("error", "Nieznana akcja: " + action);
                            break;
                    }
                } catch (Exception e) {
                    out.put("ok", false);
                    out.put("error", "Błąd akcji: " + e.getMessage());
                }
                future.complete(out);
            });

            try {
                Map<String, Object> res = future.get(5, TimeUnit.SECONDS);
                sendJson(exchange, 200, res);
            } catch (Exception e) {
                sendJson(exchange, 500, Collections.singletonMap("error", e.getMessage()));
            }
        }
    }

    private class PropertiesHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (handleCors(exchange)) return;
            if (!isAuthorized(exchange)) {
                sendJson(exchange, 401, Collections.singletonMap("error", "Unauthorized"));
                return;
            }

            if ("GET".equalsIgnoreCase(exchange.getRequestMethod())) {
                Map<String, String> props = PropertiesManager.getProperties();
                Map<String, Object> res = new LinkedHashMap<>();
                res.put("ok", true);
                res.put("properties", props);
                sendJson(exchange, 200, res);
                return;
            }

            if ("POST".equalsIgnoreCase(exchange.getRequestMethod())) {
                String body = readBody(exchange);
                Map<String, String> updates = JsonHelper.extractStringMap(body, "properties");
                if (updates.isEmpty()) {
                    // Try direct key-value flat object if "properties" wrapper wasn't used
                    updates = JsonHelper.extractStringMap("{\"properties\":" + body + "}", "properties");
                }

                boolean success = PropertiesManager.updateProperties(updates);
                Map<String, Object> res = new LinkedHashMap<>();
                res.put("ok", success);
                res.put("properties", PropertiesManager.getProperties());
                if (!success) res.put("error", "Nie udało się zapisać server.properties.");
                sendJson(exchange, success ? 200 : 500, res);
                return;
            }

            sendJson(exchange, 405, Collections.singletonMap("error", "Method Not Allowed"));
        }
    }

    private class FilesListHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (handleCors(exchange)) return;
            if (!isAuthorized(exchange)) {
                sendJson(exchange, 401, Collections.singletonMap("error", "Unauthorized"));
                return;
            }
            Map<String, String> params = getQueryParams(exchange);
            String path = params.getOrDefault("path", "/");
            try {
                List<Map<String, Object>> files = FilesManagerBridge.listDirectory(path);
                Map<String, Object> res = new LinkedHashMap<>();
                res.put("ok", true);
                res.put("path", path);
                res.put("files", files);
                sendJson(exchange, 200, res);
            } catch (Exception e) {
                sendJson(exchange, 500, Collections.singletonMap("error", e.getMessage()));
            }
        }
    }

    private class FilesReadHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (handleCors(exchange)) return;
            if (!isAuthorized(exchange)) {
                sendJson(exchange, 401, Collections.singletonMap("error", "Unauthorized"));
                return;
            }
            Map<String, String> params = getQueryParams(exchange);
            String path = params.get("path");
            if (path == null) {
                sendJson(exchange, 400, Collections.singletonMap("error", "Missing 'path' parameter"));
                return;
            }
            try {
                String content = FilesManagerBridge.readFileText(path, 5 * 1024 * 1024);
                Map<String, Object> res = new LinkedHashMap<>();
                res.put("ok", true);
                res.put("path", path);
                res.put("content", content);
                sendJson(exchange, 200, res);
            } catch (Exception e) {
                sendJson(exchange, 500, Collections.singletonMap("error", e.getMessage()));
            }
        }
    }

    private class FilesWriteHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (handleCors(exchange)) return;
            if (!isAuthorized(exchange)) {
                sendJson(exchange, 401, Collections.singletonMap("error", "Unauthorized"));
                return;
            }
            if (!"POST".equalsIgnoreCase(exchange.getRequestMethod())) {
                sendJson(exchange, 405, Collections.singletonMap("error", "Method Not Allowed"));
                return;
            }
            String body = readBody(exchange);
            String path = JsonHelper.extractString(body, "path");
            String content = JsonHelper.extractString(body, "content");
            if (path == null || content == null) {
                sendJson(exchange, 400, Collections.singletonMap("error", "Missing 'path' or 'content'"));
                return;
            }
            try {
                FilesManagerBridge.writeFileText(path, content);
                Map<String, Object> res = new LinkedHashMap<>();
                res.put("ok", true);
                res.put("path", path);
                sendJson(exchange, 200, res);
            } catch (Exception e) {
                sendJson(exchange, 500, Collections.singletonMap("error", e.getMessage()));
            }
        }
    }

    private class FilesDeleteHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (handleCors(exchange)) return;
            if (!isAuthorized(exchange)) {
                sendJson(exchange, 401, Collections.singletonMap("error", "Unauthorized"));
                return;
            }
            String body = readBody(exchange);
            String path = JsonHelper.extractString(body, "path");
            if (path == null) {
                sendJson(exchange, 400, Collections.singletonMap("error", "Missing 'path'"));
                return;
            }
            try {
                boolean del = FilesManagerBridge.deleteFileOrDir(path);
                Map<String, Object> res = new LinkedHashMap<>();
                res.put("ok", del);
                sendJson(exchange, 200, res);
            } catch (Exception e) {
                sendJson(exchange, 500, Collections.singletonMap("error", e.getMessage()));
            }
        }
    }

    private class FilesMkdirHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (handleCors(exchange)) return;
            if (!isAuthorized(exchange)) {
                sendJson(exchange, 401, Collections.singletonMap("error", "Unauthorized"));
                return;
            }
            String body = readBody(exchange);
            String path = JsonHelper.extractString(body, "path");
            if (path == null) {
                sendJson(exchange, 400, Collections.singletonMap("error", "Missing 'path'"));
                return;
            }
            try {
                boolean ok = FilesManagerBridge.createDirectory(path);
                Map<String, Object> res = new LinkedHashMap<>();
                res.put("ok", ok);
                sendJson(exchange, 200, res);
            } catch (Exception e) {
                sendJson(exchange, 500, Collections.singletonMap("error", e.getMessage()));
            }
        }
    }

    private class PapiStatusHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (handleCors(exchange)) return;
            if (!isAuthorized(exchange)) {
                sendJson(exchange, 401, Collections.singletonMap("error", "Unauthorized"));
                return;
            }
            boolean avail = PlaceholderApiBridge.isAvailable();
            Map<String, Object> res = new LinkedHashMap<>();
            res.put("ok", true);
            res.put("enabled", avail);
            res.put("version", PlaceholderApiBridge.getVersion());
            res.put("expansions", PlaceholderApiBridge.getRegisteredIdentifiers());
            sendJson(exchange, 200, res);
        }
    }

    private class PapiParseHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (handleCors(exchange)) return;
            if (!isAuthorized(exchange)) {
                sendJson(exchange, 401, Collections.singletonMap("error", "Unauthorized"));
                return;
            }
            if (!"POST".equalsIgnoreCase(exchange.getRequestMethod())) {
                sendJson(exchange, 405, Collections.singletonMap("error", "Method Not Allowed"));
                return;
            }
            String body = readBody(exchange);
            String player = JsonHelper.extractString(body, "player");
            List<String> placeholders = JsonHelper.extractStringList(body, "placeholders");

            if (placeholders.isEmpty()) {
                String singlePh = JsonHelper.extractString(body, "placeholder");
                if (singlePh != null) placeholders.add(singlePh);
            }

            Map<String, String> results = PlaceholderApiBridge.parsePlaceholders(player, placeholders);
            Map<String, Object> res = new LinkedHashMap<>();
            res.put("ok", true);
            res.put("player", player);
            res.put("papi_enabled", PlaceholderApiBridge.isAvailable());
            res.put("results", results);
            sendJson(exchange, 200, res);
        }
    }

    private class ActionHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (handleCors(exchange)) return;
            if (!isAuthorized(exchange)) {
                sendJson(exchange, 401, Collections.singletonMap("error", "Unauthorized"));
                return;
            }
            if (!"POST".equalsIgnoreCase(exchange.getRequestMethod())) {
                sendJson(exchange, 405, Collections.singletonMap("error", "Method Not Allowed"));
                return;
            }

            String body = readBody(exchange);
            String action = JsonHelper.extractString(body, "action");
            if (action == null) {
                sendJson(exchange, 400, Collections.singletonMap("error", "Missing 'action' parameter"));
                return;
            }

            Map<String, Object> res = new LinkedHashMap<>();
            switch (action.toLowerCase()) {
                case "save":
                    Bukkit.getScheduler().runTask(plugin, () -> {
                        for (World w : Bukkit.getWorlds()) {
                            w.save();
                        }
                    });
                    res.put("ok", true);
                    res.put("message", "Worlds saved successfully.");
                    break;
                case "stop":
                    Bukkit.getScheduler().runTask(plugin, Bukkit::shutdown);
                    res.put("ok", true);
                    res.put("message", "Server shutdown initiated.");
                    break;
                case "whitelist_toggle":
                    Bukkit.getScheduler().runTask(plugin, () -> Bukkit.setWhitelist(!Bukkit.hasWhitelist()));
                    res.put("ok", true);
                    res.put("whitelist", !Bukkit.hasWhitelist());
                    break;
                case "reload":
                    plugin.reloadConfig();
                    PlaceholderApiBridge.invalidate();
                    res.put("ok", true);
                    res.put("message", "DeployraBridge configuration reloaded.");
                    break;
                default:
                    sendJson(exchange, 400, Collections.singletonMap("error", "Unknown action: " + action));
                    return;
            }
            sendJson(exchange, 200, res);
        }
    }

    private class LogsHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (handleCors(exchange)) return;
            if (!isAuthorized(exchange)) {
                sendJson(exchange, 401, Collections.singletonMap("error", "Unauthorized"));
                return;
            }
            List<String> lines = logCapture != null ? logCapture.getRecentLogs() : Collections.emptyList();
            Map<String, Object> res = new LinkedHashMap<>();
            res.put("ok", true);
            res.put("count", lines.size());
            res.put("lines", lines);
            sendJson(exchange, 200, res);
        }
    }

    // --- UTILITIES ---

    private double[] getRecentTps() {
        try {
            Method method = Bukkit.getServer().getClass().getMethod("getTPS");
            return (double[]) method.invoke(Bukkit.getServer());
        } catch (Throwable ignored) {}

        try {
            org.bukkit.Server server = Bukkit.getServer();
            Field consoleField = server.getClass().getDeclaredField("console");
            consoleField.setAccessible(true);
            Object console = consoleField.get(server);
            Field tpsField = console.getClass().getField("recentTps");
            return (double[]) tpsField.get(console);
        } catch (Throwable ignored) {}

        return new double[]{20.0, 20.0, 20.0};
    }

    private int getPlayerPing(Player player) {
        try {
            Method m = player.getClass().getMethod("getPing");
            return (int) m.invoke(player);
        } catch (Throwable ignored) {}

        try {
            Method getHandle = player.getClass().getMethod("getHandle");
            Object handle = getHandle.invoke(player);
            Field pingField = handle.getClass().getDeclaredField("ping");
            return pingField.getInt(handle);
        } catch (Throwable ignored) {}

        return -1;
    }
}
