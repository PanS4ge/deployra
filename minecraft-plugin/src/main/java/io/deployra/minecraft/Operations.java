package io.deployra.minecraft;

import java.io.File;
import java.lang.reflect.Method;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Callable;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;
import org.bukkit.Bukkit;
import org.bukkit.GameMode;
import org.bukkit.Location;
import org.bukkit.OfflinePlayer;
import org.bukkit.entity.Player;
import org.bukkit.plugin.Plugin;
import org.bukkit.plugin.java.JavaPlugin;

/** Polecenia z panelu. Wszystko, co dotyka API Bukkita, idzie przez główny wątek serwera. */
final class Operations {
    private static final Pattern PLAYER = Pattern.compile("^[A-Za-z0-9_]{1,16}$");
    private final DeployraPlugin plugin;
    private final ConsoleCapture console;
    private final ServerFiles files;
    private final ServerProperties properties;

    Operations(DeployraPlugin plugin, ConsoleCapture console, ServerFiles files, ServerProperties properties) {
        this.plugin = plugin;
        this.console = console;
        this.files = files;
        this.properties = properties;
    }

    Map<String, Object> run(String op, Map<String, Object> args) throws Exception {
        if ("command".equals(op)) {
            return command(Json.str(args.get("command"), ""));
        }
        if ("logs".equals(op)) {
            long since = args.get("since") instanceof Number ? ((Number) args.get("since")).longValue() : 0L;
            Map<String, Object> out = new LinkedHashMap<String, Object>();
            List<String> lines = console.since(since, 300);
            out.put("lines", lines);
            out.put("next", (since <= 0 ? console.position() - lines.size() : since) + lines.size());
            return out;
        }
        if ("players".equals(op)) {
            return sync(new Callable<Map<String, Object>>() {
                @Override
                public Map<String, Object> call() {
                    return players();
                }
            });
        }
        if ("player".equals(op)) {
            return player(Json.str(args.get("name"), ""), Json.str(args.get("action"), ""), Json.str(args.get("reason"), ""));
        }
        if ("properties.get".equals(op)) {
            return one("properties", properties.read());
        }
        if ("properties.set".equals(op)) {
            properties.write(Json.object(args.get("properties")));
            return new LinkedHashMap<String, Object>();
        }
        if ("files.list".equals(op)) {
            return one("items", files.list(Json.str(args.get("path"), "/")));
        }
        if ("files.read".equals(op)) {
            return one("data_b64", files.read(Json.str(args.get("path"), "")));
        }
        if ("files.write".equals(op)) {
            files.write(Json.str(args.get("path"), ""), Json.str(args.get("data_b64"), ""));
            return new LinkedHashMap<String, Object>();
        }
        if ("files.write_chunk".equals(op)) {
            long size = files.writeChunk(Json.str(args.get("path"), ""), Json.str(args.get("upload"), ""), num(args.get("offset")),
                    Json.str(args.get("data_b64"), ""));
            return one("size", size);
        }
        if ("files.commit".equals(op)) {
            files.commit(Json.str(args.get("path"), ""), Json.str(args.get("upload"), ""), num(args.get("size")), Json.str(args.get("sha256"), ""));
            return new LinkedHashMap<String, Object>();
        }
        if ("files.abort".equals(op)) {
            files.abort(Json.str(args.get("path"), ""), Json.str(args.get("upload"), ""));
            return new LinkedHashMap<String, Object>();
        }
        if ("files.read_chunk".equals(op)) {
            return files.readChunk(Json.str(args.get("path"), ""), num(args.get("offset")), num(args.get("length")));
        }
        if ("files.delete".equals(op)) {
            files.delete(Json.str(args.get("path"), ""));
            return new LinkedHashMap<String, Object>();
        }
        if ("files.mkdir".equals(op)) {
            files.mkdir(Json.str(args.get("path"), ""));
            return new LinkedHashMap<String, Object>();
        }
        if ("plugins.list".equals(op)) {
            return sync(new Callable<Map<String, Object>>() {
                @Override
                public Map<String, Object> call() {
                    return one("plugins", pluginList());
                }
            });
        }
        if ("plugins.disable".equals(op) || "plugins.enable".equals(op)) {
            final boolean enable = "plugins.enable".equals(op);
            final String name = Json.str(args.get("name"), "");
            final String file = Json.str(args.get("file"), "");
            final boolean persist = Boolean.TRUE.equals(args.get("persist"));
            return sync(new Callable<Map<String, Object>>() {
                @Override
                public Map<String, Object> call() throws Exception {
                    return enable ? enablePlugin(name, file) : disablePlugin(name, persist);
                }
            });
        }
        if ("papi".equals(op)) {
            final String text = Json.str(args.get("text"), "");
            final String name = Json.str(args.get("player"), "");
            return sync(new Callable<Map<String, Object>>() {
                @Override
                public Map<String, Object> call() throws Exception {
                    return placeholders(text, name);
                }
            });
        }
        throw new IllegalArgumentException("unknown operation: " + op);
    }

    /** Informacje do listy serwerów w panelu (zbierane na głównym wątku co kilkanaście sekund). */
    Map<String, Object> info() {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        out.put("software", Bukkit.getName());
        out.put("mc_version", Bukkit.getBukkitVersion().split("-")[0]);
        out.put("plugin_version", plugin.getDescription().getVersion());
        out.put("features", java.util.Arrays.asList("files.chunks", "plugins.manage"));  // co ten plugin umie ponad 1.1
        out.put("players_online", Bukkit.getOnlinePlayers().size());
        out.put("players_max", Bukkit.getMaxPlayers());
        out.put("motd", Bukkit.getMotd());
        out.put("papi", Bukkit.getPluginManager().getPlugin("PlaceholderAPI") != null);
        try {
            double[] tps = (double[]) Bukkit.class.getMethod("getTPS").invoke(null);  // tylko Paper
            out.put("tps", Math.round(Math.min(20.0, tps[0]) * 10) / 10.0);
        } catch (Throwable ignored) {
            // Spigot bez TPS
        }
        return out;
    }

    private Map<String, Object> command(final String line) throws Exception {
        if (line.isEmpty() || line.length() > 1000 || line.indexOf('\n') >= 0 || line.indexOf('\r') >= 0) {
            throw new IllegalArgumentException("bad command");
        }
        long mark = console.position();
        final String cmd = line.startsWith("/") ? line.substring(1) : line;
        Boolean known = sync(new Callable<Boolean>() {
            @Override
            public Boolean call() {
                return Bukkit.dispatchCommand(Bukkit.getConsoleSender(), cmd);
            }
        });
        Thread.sleep(250);  // log4j bywa asynchroniczny — dajemy chwilę na dopisanie wyniku
        List<String> output = new ArrayList<String>(console.since(mark, 200));
        if (output.isEmpty() && !console.usesLog4j()) {
            output.add(Boolean.TRUE.equals(known) ? "OK" : "Unknown command");
        }
        return one("output", output);
    }

    private Map<String, Object> players() {
        List<Map<String, Object>> out = new ArrayList<Map<String, Object>>();
        for (Player p : Bukkit.getOnlinePlayers()) {
            Map<String, Object> item = new LinkedHashMap<String, Object>();
            item.put("name", p.getName());
            item.put("uuid", p.getUniqueId().toString());
            item.put("health", p.getHealth());
            item.put("max_health", maxHealth(p));
            item.put("food", p.getFoodLevel());
            item.put("gamemode", p.getGameMode().name().toLowerCase());
            item.put("op", p.isOp());
            item.put("whitelisted", p.isWhitelisted());
            Location loc = p.getLocation();
            item.put("world", loc.getWorld() == null ? "" : loc.getWorld().getName());
            item.put("x", loc.getBlockX());
            item.put("y", loc.getBlockY());
            item.put("z", loc.getBlockZ());
            item.put("ping", ping(p));
            out.add(item);
        }
        return one("players", out);
    }

    @SuppressWarnings("deprecation")
    private static double maxHealth(Player p) {
        try {
            return p.getMaxHealth();
        } catch (Throwable e) {
            return 20.0;
        }
    }

    private static Object ping(Player p) {
        try {
            return p.getClass().getMethod("getPing").invoke(p);  // 1.17+
        } catch (Throwable e) {
            return null;
        }
    }

    private Map<String, Object> player(final String name, final String action, String reason) throws Exception {
        if (!PLAYER.matcher(name).matches()) {
            throw new IllegalArgumentException("bad player name");
        }
        String clean = reason.replace('\n', ' ').replace('\r', ' ');
        if (clean.length() > 120) {
            clean = clean.substring(0, 120);
        }
        String vanilla = null;
        if ("op".equals(action)) {
            vanilla = "op " + name;
        } else if ("deop".equals(action)) {
            vanilla = "deop " + name;
        } else if ("whitelist_add".equals(action)) {
            vanilla = "whitelist add " + name;
        } else if ("whitelist_remove".equals(action)) {
            vanilla = "whitelist remove " + name;
        } else if ("kick".equals(action)) {
            vanilla = ("kick " + name + " " + clean).trim();
        } else if ("ban".equals(action)) {
            vanilla = ("ban " + name + " " + clean).trim();
        } else if ("pardon".equals(action)) {
            vanilla = "pardon " + name;
        }
        if (vanilla != null) {
            Map<String, Object> out = command(vanilla);
            List<Object> lines = Json.list(out.get("output"));
            out.put("message", lines.isEmpty() ? vanilla : String.valueOf(lines.get(lines.size() - 1)));
            return out;
        }
        return sync(new Callable<Map<String, Object>>() {
            @Override
            public Map<String, Object> call() {
                Player p = Bukkit.getPlayerExact(name);
                if (p == null) {
                    throw new IllegalStateException("player is offline");
                }
                if ("survival".equals(action) || "creative".equals(action) || "adventure".equals(action) || "spectator".equals(action)) {
                    p.setGameMode(GameMode.valueOf(action.toUpperCase()));
                } else if ("heal".equals(action)) {
                    p.setHealth(maxHealth(p));
                    p.setFireTicks(0);
                } else if ("feed".equals(action)) {
                    p.setFoodLevel(20);
                    p.setSaturation(20f);
                } else if ("kill".equals(action)) {
                    p.setHealth(0);
                } else if ("starve".equals(action)) {
                    p.setFoodLevel(0);
                } else {
                    throw new IllegalArgumentException("unknown action");
                }
                return one("message", name + ": " + action);
            }
        });
    }

    private Map<String, Object> placeholders(String text, String name) throws Exception {
        Plugin papi = Bukkit.getPluginManager().getPlugin("PlaceholderAPI");
        if (papi == null) {
            return one("installed", false);
        }
        OfflinePlayer target = null;
        if (!name.isEmpty()) {
            if (!PLAYER.matcher(name).matches()) {
                throw new IllegalArgumentException("bad player name");
            }
            target = Bukkit.getPlayerExact(name);  // tylko gracz online — bez zapytań do Mojang na głównym wątku
        }
        Class<?> api = Class.forName("me.clip.placeholderapi.PlaceholderAPI", true, papi.getClass().getClassLoader());
        Method set = api.getMethod("setPlaceholders", OfflinePlayer.class, String.class);
        Map<String, Object> out = one("installed", true);
        out.put("result", String.valueOf(set.invoke(null, target, text)));
        return out;
    }

    // --- pluginy serwera: lista, wyłączanie (od razu albo na stałe: jar → .jar.disabled) i włączanie --------------------

    private static final String DISABLED = ".disabled";

    private File pluginsDir() {
        return plugin.getDataFolder().getParentFile();
    }

    /** Plik jar pluginu — JavaPlugin#getFile jest chronione, więc przez refleksję (bez niego tylko wyłączanie do restartu). */
    private static File jarOf(Plugin p) {
        try {
            Method getFile = JavaPlugin.class.getDeclaredMethod("getFile");
            getFile.setAccessible(true);
            return (File) getFile.invoke(p);
        } catch (Throwable e) {
            return null;
        }
    }

    private List<Map<String, Object>> pluginList() {
        List<Map<String, Object>> out = new ArrayList<Map<String, Object>>();
        for (Plugin p : Bukkit.getPluginManager().getPlugins()) {
            File jar = jarOf(p);
            Map<String, Object> item = new LinkedHashMap<String, Object>();
            item.put("name", p.getName());
            item.put("version", p.getDescription().getVersion());
            item.put("description", p.getDescription().getDescription());
            item.put("authors", p.getDescription().getAuthors());
            item.put("enabled", p.isEnabled());
            item.put("file", jar != null ? jar.getName() : null);
            item.put("persisted", jar != null && !jar.isFile() && new File(jar.getPath() + DISABLED).isFile());
            item.put("self", p == plugin);
            out.add(item);
        }
        File[] files = pluginsDir().listFiles();
        for (File f : files == null ? new File[0] : files) {  // wyłączone na stałe i niewczytane (po restarcie)
            String name = f.getName();
            if (!f.isFile() || !name.toLowerCase(java.util.Locale.ROOT).endsWith(".jar" + DISABLED)) {
                continue;
            }
            boolean loaded = false;
            for (Map<String, Object> item : out) {
                loaded |= (name.substring(0, name.length() - DISABLED.length())).equals(item.get("file"));
            }
            if (!loaded) {
                Map<String, Object> item = new LinkedHashMap<String, Object>();
                item.put("name", name.substring(0, name.length() - (".jar" + DISABLED).length()));
                item.put("enabled", false);
                item.put("file", name.substring(0, name.length() - DISABLED.length()));
                item.put("persisted", true);
                item.put("loaded", false);
                out.add(item);
            }
        }
        return out;
    }

    private Map<String, Object> disablePlugin(String name, boolean persist) throws Exception {
        Plugin p = Bukkit.getPluginManager().getPlugin(name);
        if (p == null) {
            throw new IllegalArgumentException("no such plugin: " + name);
        }
        if (p == plugin) {
            throw new IllegalArgumentException("refusing to disable DeployraBridge: it connects this server to the panel");
        }
        if (p.isEnabled()) {
            Bukkit.getPluginManager().disablePlugin(p);
        }
        if (persist) {
            File jar = jarOf(p);
            if (jar == null || !jar.isFile()) {
                throw new java.io.IOException("plugin file not found: " + name);
            }
            if (!jar.renameTo(new File(jar.getPath() + DISABLED))) {
                throw new java.io.IOException("could not rename " + jar.getName());
            }
        }
        return one("restart", false);
    }

    /** Wyłączony do restartu → od razu z powrotem; wyłączony na stałe → jar wraca na miejsce (niewczytany plugin dopiero po restarcie). */
    private Map<String, Object> enablePlugin(String name, String file) throws Exception {
        Plugin p = name.isEmpty() ? null : Bukkit.getPluginManager().getPlugin(name);
        File jar = p != null ? jarOf(p) : null;
        if (jar == null && !file.isEmpty()) {
            jar = new File(pluginsDir(), new File(file).getName());  // tylko nazwa — bez wychodzenia poza plugins/
        }
        if (jar != null && !jar.isFile()) {
            File disabled = new File(jar.getPath() + DISABLED);
            if (disabled.isFile() && !disabled.renameTo(jar)) {
                throw new java.io.IOException("could not rename " + disabled.getName());
            }
        }
        if (p == null) {
            if (jar == null || !jar.isFile()) {
                throw new IllegalArgumentException("no such plugin: " + (name.isEmpty() ? file : name));
            }
            return one("restart", true);
        }
        if (!p.isEnabled()) {
            Bukkit.getPluginManager().enablePlugin(p);
        }
        return one("restart", false);
    }

    private <T> T sync(Callable<T> task) throws Exception {
        if (Bukkit.isPrimaryThread()) {
            return task.call();
        }
        return Bukkit.getScheduler().callSyncMethod(plugin, task).get(15, TimeUnit.SECONDS);
    }

    private static Map<String, Object> one(String key, Object value) {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        out.put(key, value);
        return out;
    }

    private static long num(Object value) {
        return value instanceof Number ? ((Number) value).longValue() : 0L;
    }
}
