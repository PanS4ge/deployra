package io.deployra.minecraft;

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
        if ("files.delete".equals(op)) {
            files.delete(Json.str(args.get("path"), ""));
            return new LinkedHashMap<String, Object>();
        }
        if ("files.mkdir".equals(op)) {
            files.mkdir(Json.str(args.get("path"), ""));
            return new LinkedHashMap<String, Object>();
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
}
