package io.deployra.minecraft;

import java.io.File;
import java.util.Map;
import org.bukkit.Bukkit;
import org.bukkit.ChatColor;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.command.ConsoleCommandSender;
import org.bukkit.plugin.java.JavaPlugin;

/**
 * DeployraBridge — zarządzanie serwerem Minecraft z panelu Deployra.
 * Plugin łączy się z panelem sam (żaden port na serwerze nie jest otwierany); parowanie: /deployra pair <adres> <kod>.
 */
public final class DeployraPlugin extends JavaPlugin {
    private ConsoleCapture console;
    private BridgeClient bridge;
    private Operations operations;

    @Override
    public void onEnable() {
        saveDefaultConfig();
        console = new ConsoleCapture(getConfig().getInt("max-log-lines", 1000));
        console.install(Bukkit.getServer().getClass().getClassLoader());
        File serverDir = new File(".").getAbsoluteFile();
        try {
            operations = new Operations(this, console, new ServerFiles(serverDir), new ServerProperties(serverDir));
        } catch (Exception e) {
            getLogger().severe("Cannot open the server directory: " + e.getMessage());
            getServer().getPluginManager().disablePlugin(this);
            return;
        }
        bridge = new BridgeClient(this, operations);
        bridge.start();
        Bukkit.getScheduler().runTaskTimer(this, new Runnable() {
            @Override
            public void run() {
                bridge.setInfo(operations.info());
            }
        }, 20L, 300L);
        String code = System.getenv("DEPLOYRA_PAIR_CODE");
        String url = System.getenv("DEPLOYRA_PANEL_URL");
        if (!paired() && code != null && !code.isEmpty() && !"auto".equals(code) && url != null && url.startsWith("http")) {
            pairAsync(url, code, Bukkit.getConsoleSender());  // serwer postawiony z panelu (aplikacja) paruje się sam
        }
        getLogger().info(paired() ? "Deployra: paired with " + panelUrl() : "Deployra: not paired — use /deployra pair <panel-url> <code>");
    }

    @Override
    public void onDisable() {
        if (bridge != null) {
            bridge.stop();
        }
        if (console != null) {
            console.uninstall();
        }
    }

    boolean paired() {
        return !secret().isEmpty() && serverId() > 0 && panelUrl().startsWith("http");
    }

    String panelUrl() {
        return getConfig().getString("panel-url", "");
    }

    String secret() {
        return getConfig().getString("secret", "");
    }

    int serverId() {
        return getConfig().getInt("server-id", 0);
    }

    private void pairAsync(final String url, final String code, final CommandSender sender) {
        final Map<String, Object> info = operations.info();
        Bukkit.getScheduler().runTaskAsynchronously(this, new Runnable() {
            @Override
            public void run() {
                try {
                    final Map<String, Object> answer = BridgeClient.pair(url, code, info);
                    Bukkit.getScheduler().runTask(DeployraPlugin.this, new Runnable() {
                        @Override
                        public void run() {
                            getConfig().set("panel-url", url.replaceAll("/+$", ""));
                            getConfig().set("server-id", ((Number) answer.get("server_id")).intValue());
                            getConfig().set("secret", Json.str(answer.get("secret"), ""));
                            saveConfig();
                            sender.sendMessage(ChatColor.GREEN + "Deployra: paired.");
                        }
                    });
                } catch (Exception e) {
                    sender.sendMessage(ChatColor.RED + "Deployra: pairing failed — " + e.getMessage());
                }
            }
        });
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!(sender instanceof ConsoleCommandSender) && !sender.isOp()) {
            sender.sendMessage(ChatColor.RED + "Deployra: console or OP only.");
            return true;
        }
        String sub = args.length > 0 ? args[0].toLowerCase() : "status";
        if ("pair".equals(sub)) {
            if (args.length != 3 || !args[1].startsWith("http")) {
                sender.sendMessage(ChatColor.YELLOW + "/deployra pair <panel-url> <code>");
                return true;
            }
            if (args[1].startsWith("http://")) {
                sender.sendMessage(ChatColor.YELLOW + "Deployra: the panel URL is not HTTPS — commands are still signed, but use HTTPS if you can.");
            }
            sender.sendMessage("Deployra: pairing…");
            pairAsync(args[1], args[2], sender);
        } else if ("unpair".equals(sub)) {
            getConfig().set("secret", "");
            getConfig().set("server-id", 0);
            saveConfig();
            sender.sendMessage(ChatColor.YELLOW + "Deployra: unpaired.");
        } else {
            sender.sendMessage("Deployra " + getDescription().getVersion() + ": " + bridge.status() + (paired() ? " (" + panelUrl() + ")" : ""));
        }
        return true;
    }
}
