package io.deployra.minecraft;

import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.util.ArrayList;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.List;
import java.util.logging.Handler;
import java.util.logging.LogRecord;
import java.util.regex.Pattern;

/**
 * Bufor ostatnich linii konsoli serwera z numerami (konsola w panelu, wynik komend).
 * Podpina się do log4j2 (Minecraft 1.7+) przez Proxy interfejsu Appender — bez zależności przy kompilacji;
 * gdy log4j nie jest dostępny, zostaje handler java.util.logging.
 */
final class ConsoleCapture {
    private static final Pattern ANSI = Pattern.compile("\u001B\\[[;\\d]*[A-Za-z]");
    private static final Pattern COLORS = Pattern.compile("(?i)§[0-9A-FK-ORX]");

    private final int max;
    private final Deque<String> lines = new ArrayDeque<String>();
    private long next = 1;
    private Object appender;
    private Object rootLogger;
    private Handler julHandler;

    ConsoleCapture(int max) {
        this.max = Math.max(50, max);
    }

    synchronized void add(String line) {
        if (line == null) {
            return;
        }
        for (String part : COLORS.matcher(ANSI.matcher(line).replaceAll("")).replaceAll("").split("\n")) {
            lines.addLast(part);
            next++;
            while (lines.size() > max) {
                lines.removeFirst();
            }
        }
    }

    synchronized long position() {
        return next;
    }

    /** Linie o numerach od `since` (pierwsze wywołanie z 0 = ostatnie 200). */
    synchronized List<String> since(long since, int limit) {
        long first = next - lines.size();
        long from = since <= 0 ? Math.max(first, next - 200) : Math.max(since, first);
        List<String> out = new ArrayList<String>();
        long index = first;
        for (String line : lines) {
            if (index >= from && out.size() < limit) {
                out.add(line);
            }
            index++;
        }
        return out;
    }

    void install(ClassLoader serverLoader) {
        try {
            Class<?> logManager = Class.forName("org.apache.logging.log4j.LogManager", true, serverLoader);
            rootLogger = logManager.getMethod("getRootLogger").invoke(null);
            ClassLoader loader = rootLogger.getClass().getClassLoader();
            Class<?> appenderType = Class.forName("org.apache.logging.log4j.core.Appender", true, loader);
            final Object started = Enum.valueOf(enumClass(loader), "STARTED");
            appender = Proxy.newProxyInstance(loader, new Class<?>[] {appenderType}, new InvocationHandler() {
                @Override
                public Object invoke(Object proxy, Method method, Object[] args) throws Throwable {
                    String name = method.getName();
                    if ("append".equals(name)) {
                        Object event = args[0];
                        Object message = event.getClass().getMethod("getMessage").invoke(event);
                        add(message == null ? null : String.valueOf(message.getClass().getMethod("getFormattedMessage").invoke(message)));
                        return null;
                    }
                    if ("getName".equals(name)) {
                        return "DeployraBridge";
                    }
                    if ("isStarted".equals(name) || "ignoreExceptions".equals(name)) {
                        return true;
                    }
                    if ("isStopped".equals(name)) {
                        return false;
                    }
                    if ("getState".equals(name)) {
                        return started;
                    }
                    if ("hashCode".equals(name)) {
                        return System.identityHashCode(proxy);
                    }
                    if ("equals".equals(name)) {
                        return proxy == args[0];
                    }
                    if ("toString".equals(name)) {
                        return "DeployraBridgeAppender";
                    }
                    return null; // start, stop, initialize, setHandler, getLayout, getHandler
                }
            });
            rootLogger.getClass().getMethod("addAppender", appenderType).invoke(rootLogger, appender);
            return;
        } catch (Throwable ignored) {
            appender = null;
        }
        julHandler = new Handler() {
            @Override
            public void publish(LogRecord record) {
                add(record.getMessage());
            }

            @Override
            public void flush() {
            }

            @Override
            public void close() {
            }
        };
        java.util.logging.Logger.getLogger("").addHandler(julHandler);
    }

    @SuppressWarnings({"unchecked", "rawtypes"})
    private static Class<Enum> enumClass(ClassLoader loader) throws ClassNotFoundException {
        return (Class<Enum>) Class.forName("org.apache.logging.log4j.core.LifeCycle$State", true, loader);
    }

    boolean usesLog4j() {
        return appender != null;
    }

    void uninstall() {
        try {
            if (appender != null) {
                for (Method m : rootLogger.getClass().getMethods()) {
                    if ("removeAppender".equals(m.getName()) && m.getParameterTypes().length == 1) {
                        m.invoke(rootLogger, appender);
                    }
                }
            }
        } catch (Throwable ignored) {
            // serwer się wyłącza — nic do sprzątania
        }
        if (julHandler != null) {
            java.util.logging.Logger.getLogger("").removeHandler(julHandler);
        }
    }
}
