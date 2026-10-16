package io.deployra.minecraft;

import org.bukkit.Bukkit;
import org.bukkit.command.CommandSender;

import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * Universal dynamic proxy CommandSender that captures output across all Minecraft versions (1.8 - 1.21+)
 * without compile-time dependencies on Spigot/Paper specific Adventure interfaces.
 */
public class CommandCaptureSender {

    private final List<String> output = Collections.synchronizedList(new ArrayList<>());
    private final CommandSender proxy;

    public CommandCaptureSender() {
        CommandSender console = Bukkit.getConsoleSender();
        InvocationHandler handler = new InvocationHandler() {
            @Override
            public Object invoke(Object proxyObj, Method method, Object[] args) throws Throwable {
                String methodName = method.getName();

                if (methodName.equals("sendMessage") || methodName.equals("sendRawMessage")) {
                    if (args != null && args.length > 0) {
                        for (Object arg : args) {
                            if (arg instanceof String) {
                                output.add(stripColors((String) arg));
                            } else if (arg instanceof String[]) {
                                for (String s : (String[]) arg) {
                                    output.add(stripColors(s));
                                }
                            } else if (arg != null) {
                                // Handles Kyori Adventure Component, BaseComponent, etc. via toString or PlainText
                                output.add(stripColors(arg.toString()));
                            }
                        }
                    }
                    return null;
                }

                if (methodName.equals("getName")) {
                    return "DEPLOYRA";
                }

                if (methodName.equals("isOp") || methodName.startsWith("hasPermission") || methodName.startsWith("isPermissionSet")) {
                    return true;
                }

                if (console != null) {
                    try {
                        return method.invoke(console, args);
                    } catch (Throwable ignored) {}
                }

                Class<?> ret = method.getReturnType();
                if (ret == boolean.class) return true;
                if (ret == int.class) return 0;
                if (ret == long.class) return 0L;
                if (ret == double.class) return 0.0;
                return null;
            }
        };

        this.proxy = (CommandSender) Proxy.newProxyInstance(
                CommandSender.class.getClassLoader(),
                new Class<?>[]{CommandSender.class},
                handler
        );
    }

    public CommandSender getSender() {
        return proxy;
    }

    public List<String> getOutput() {
        return new ArrayList<>(output);
    }

    public String getOutputJoined() {
        return String.join("\n", output);
    }

    private static String stripColors(String input) {
        if (input == null) return "";
        return input.replaceAll("(?i)§[0-9A-FK-ORX]", "");
    }
}
