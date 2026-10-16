package io.deployra.minecraft;

import org.bukkit.ChatColor;
import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;
import org.bukkit.plugin.java.JavaPlugin;

import java.io.File;
import java.security.SecureRandom;
import java.util.logging.Logger;

public class DeployraPlugin extends JavaPlugin implements CommandExecutor {

    private DeployraHttpServer httpServer;
    private LogCaptureHandler logCapture;
    private String currentToken;
    private int currentPort;

    @Override
    public void onEnable() {
        saveDefaultConfig();
        loadAndStartServer();

        if (getCommand("deployra") != null) {
            getCommand("deployra").setExecutor(this);
        }

        getLogger().info("==================================================");
        getLogger().info("DeployraBridge v" + getDescription().getVersion() + " has been ENABLED!");
        getLogger().info("HTTP Server port: " + currentPort);
        getLogger().info("Token: " + currentToken);
        getLogger().info("Control URL: http://<server-ip>:" + currentPort + "/health");
        getLogger().info("==================================================");
    }

    @Override
    public void onDisable() {
        if (httpServer != null) {
            httpServer.stop();
            httpServer = null;
        }

        if (logCapture != null) {
            try {
                Logger.getLogger("").removeHandler(logCapture);
                getLogger().removeHandler(logCapture);
            } catch (Exception ignored) {}
            logCapture = null;
        }

        getLogger().info("DeployraBridge has been DISABLED.");
    }

    private void loadAndStartServer() {
        reloadConfig();

        int port = getConfig().getInt("port", 25585);
        String host = getConfig().getString("host", "0.0.0.0");
        String token = getConfig().getString("token", "").trim();
        int maxLogs = getConfig().getInt("max-log-lines", 150);
        boolean remoteCommands = getConfig().getBoolean("enable-remote-commands", true);

        if (token.isEmpty()) {
            token = generateRandomToken();
            getConfig().set("token", token);
            saveConfig();
            getLogger().info("Generated new secret Deployra token: " + token);
        }

        this.currentToken = token;
        this.currentPort = port;

        // Initialize log capture
        if (logCapture == null) {
            logCapture = new LogCaptureHandler(maxLogs);
            try {
                Logger.getLogger("").addHandler(logCapture);
                getLogger().addHandler(logCapture);
            } catch (Exception e) {
                getLogger().warning("Could not attach log capture handler: " + e.getMessage());
            }
        }

        // Start HTTP server
        try {
            if (httpServer != null) {
                httpServer.stop();
            }
            httpServer = new DeployraHttpServer(this, host, port, token, remoteCommands, logCapture);
            httpServer.start();
        } catch (Exception e) {
            getLogger().severe("FAILED TO START DEPLOYRA HTTP BRIDGE: " + e.getMessage());
            e.printStackTrace();
        }
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        if (!sender.hasPermission("deployra.admin")) {
            sender.sendMessage(ChatColor.RED + "You do not have permission to use this command.");
            return true;
        }

        if (args.length == 0 || args[0].equalsIgnoreCase("status")) {
            sender.sendMessage(ChatColor.GREEN + "--- DeployraBridge Status ---");
            sender.sendMessage(ChatColor.YELLOW + "Status: " + ChatColor.WHITE + (httpServer != null ? "ACTIVE" : "STOPPED"));
            sender.sendMessage(ChatColor.YELLOW + "Port: " + ChatColor.WHITE + currentPort);
            sender.sendMessage(ChatColor.YELLOW + "Online players: " + ChatColor.WHITE + getServer().getOnlinePlayers().size());
            sender.sendMessage(ChatColor.GRAY + "Use '/deployra token' to view authorization token.");
            return true;
        }

        if (args[0].equalsIgnoreCase("token")) {
            sender.sendMessage(ChatColor.GREEN + "Deployra Secret Token: " + ChatColor.GOLD + currentToken);
            sender.sendMessage(ChatColor.GRAY + "Use this token in Deployra or API requests.");
            return true;
        }

        if (args[0].equalsIgnoreCase("reload")) {
            sender.sendMessage(ChatColor.YELLOW + "Reloading DeployraBridge configuration and restarting HTTP server...");
            loadAndStartServer();
            sender.sendMessage(ChatColor.GREEN + "DeployraBridge successfully reloaded on port " + currentPort + "!");
            return true;
        }

        sender.sendMessage(ChatColor.RED + "Usage: /deployra [status|token|reload]");
        return true;
    }

    private static String generateRandomToken() {
        SecureRandom random = new SecureRandom();
        byte[] bytes = new byte[16];
        random.nextBytes(bytes);
        StringBuilder sb = new StringBuilder("dep_");
        for (byte b : bytes) {
            sb.append(String.format("%02x", b));
        }
        return sb.toString();
    }
}
