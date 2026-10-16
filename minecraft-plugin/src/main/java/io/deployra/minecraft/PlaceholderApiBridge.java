package io.deployra.minecraft;

import org.bukkit.Bukkit;
import org.bukkit.OfflinePlayer;
import org.bukkit.entity.Player;
import org.bukkit.plugin.Plugin;

import java.lang.reflect.Method;
import java.util.*;

/**
 * Safe reflection-based bridge to PlaceholderAPI.
 * Zero compile-time dependency, zero runtime crashes if PAPI is missing.
 */
public class PlaceholderApiBridge {

    private static Boolean available = null;

    public static boolean isAvailable() {
        if (available == null) {
            Plugin p = Bukkit.getPluginManager().getPlugin("PlaceholderAPI");
            available = (p != null && p.isEnabled());
        }
        return available;
    }

    public static void invalidate() {
        available = null;
    }

    public static String getVersion() {
        if (!isAvailable()) return null;
        try {
            Plugin p = Bukkit.getPluginManager().getPlugin("PlaceholderAPI");
            return p != null ? p.getDescription().getVersion() : null;
        } catch (Throwable ignored) {
            return null;
        }
    }

    @SuppressWarnings("unchecked")
    public static List<String> getRegisteredIdentifiers() {
        if (!isAvailable()) return Collections.emptyList();
        try {
            Class<?> papiClass = Class.forName("me.clip.placeholderapi.PlaceholderAPI");
            Method method = papiClass.getMethod("getRegisteredIdentifiers");
            Object res = method.invoke(null);
            if (res instanceof Collection) {
                return new ArrayList<>((Collection<String>) res);
            }
        } catch (Throwable ignored) {}

        try {
            Class<?> papiPluginClass = Class.forName("me.clip.placeholderapi.PlaceholderAPIPlugin");
            Method getInstance = papiPluginClass.getMethod("getInstance");
            Object instance = getInstance.invoke(null);
            Method getExpManager = instance.getClass().getMethod("getLocalExpansionManager");
            Object expManager = getExpManager.invoke(instance);
            Method getExpansions = expManager.getClass().getMethod("getExpansions");
            Object exps = getExpansions.invoke(expManager);
            if (exps instanceof Map) {
                return new ArrayList<>(((Map<String, ?>) exps).keySet());
            }
        } catch (Throwable ignored) {}

        return Collections.emptyList();
    }

    public static String setPlaceholders(OfflinePlayer player, String text) {
        if (!isAvailable() || text == null) return text;
        try {
            Class<?> papiClass = Class.forName("me.clip.placeholderapi.PlaceholderAPI");
            Method method = papiClass.getMethod("setPlaceholders", OfflinePlayer.class, String.class);
            return (String) method.invoke(null, player, text);
        } catch (Throwable t) {
            return text;
        }
    }

    public static String setPlaceholders(Player player, String text) {
        if (!isAvailable() || text == null) return text;
        try {
            Class<?> papiClass = Class.forName("me.clip.placeholderapi.PlaceholderAPI");
            Method method = papiClass.getMethod("setPlaceholders", Player.class, String.class);
            return (String) method.invoke(null, player, text);
        } catch (Throwable ignored) {}
        return setPlaceholders((OfflinePlayer) player, text);
    }

    public static Map<String, String> parsePlaceholders(String playerName, List<String> placeholders) {
        Map<String, String> results = new LinkedHashMap<>();
        if (placeholders == null) return results;

        Player player = null;
        OfflinePlayer offlinePlayer = null;
        if (playerName != null && !playerName.trim().isEmpty()) {
            player = Bukkit.getPlayerExact(playerName.trim());
            if (player == null) {
                try {
                    offlinePlayer = Bukkit.getOfflinePlayer(playerName.trim());
                } catch (Throwable ignored) {}
            }
        }

        for (String ph : placeholders) {
            if (ph == null || ph.trim().isEmpty()) continue;
            String cleanPh = ph.trim();
            if (!cleanPh.startsWith("%")) cleanPh = "%" + cleanPh;
            if (!cleanPh.endsWith("%")) cleanPh = cleanPh + "%";

            String parsed;
            if (player != null) {
                parsed = setPlaceholders(player, cleanPh);
            } else if (offlinePlayer != null) {
                parsed = setPlaceholders(offlinePlayer, cleanPh);
            } else {
                parsed = setPlaceholders((Player) null, cleanPh);
            }
            results.put(cleanPh, parsed);
        }

        return results;
    }
}
