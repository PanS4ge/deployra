package io.deployra.minecraft;

import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.io.StringReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.Set;
import java.util.TreeSet;

/** server.properties: odczyt jak Minecraft (java.util.Properties), zapis linia po linii — komentarze i kolejność zostają. */
final class ServerProperties {
    private final File file;

    ServerProperties(File serverDir) {
        this.file = new File(serverDir, "server.properties");
    }

    Map<String, String> read() throws IOException {
        Properties props = new Properties();
        if (file.isFile()) {
            InputStream in = Files.newInputStream(file.toPath());
            try {
                props.load(in);
            } finally {
                in.close();
            }
        }
        Map<String, String> out = new LinkedHashMap<String, String>();
        for (String key : new TreeSet<String>(props.stringPropertyNames())) {
            out.put(key, props.getProperty(key));
        }
        return out;
    }

    void write(Map<String, Object> changes) throws IOException {
        List<String> lines = file.isFile() ? Files.readAllLines(file.toPath(), StandardCharsets.ISO_8859_1) : new ArrayList<String>();
        Set<String> done = new TreeSet<String>();
        List<String> out = new ArrayList<String>();
        for (String line : lines) {
            String trimmed = line.trim();
            int eq = line.indexOf('=');
            if (trimmed.startsWith("#") || trimmed.startsWith("!") || eq < 0) {
                out.add(line);
                continue;
            }
            String key = keyOf(line.substring(0, eq));
            if (changes.containsKey(key)) {
                out.add(escapeKey(key) + "=" + escape(String.valueOf(changes.get(key))));
                done.add(key);
            } else {
                out.add(line);
            }
        }
        for (Map.Entry<String, Object> entry : changes.entrySet()) {
            if (!done.contains(entry.getKey()) && entry.getKey().matches("[a-z0-9._-]{1,64}")) {
                out.add(escapeKey(entry.getKey()) + "=" + escape(String.valueOf(entry.getValue())));
            }
        }
        File tmp = new File(file.getParentFile(), "server.properties.deployra-tmp");
        Files.write(tmp.toPath(), out, StandardCharsets.ISO_8859_1);
        Files.move(tmp.toPath(), file.toPath(), StandardCopyOption.REPLACE_EXISTING);
    }

    private static String keyOf(String raw) throws IOException {
        Properties p = new Properties();
        p.load(new StringReader(raw.trim() + "="));
        return p.stringPropertyNames().isEmpty() ? raw.trim() : p.stringPropertyNames().iterator().next();
    }

    private static String escapeKey(String key) {
        return key.replace("\\", "\\\\").replace(" ", "\\ ").replace(":", "\\:").replace("=", "\\=");
    }

    /** Jak Properties.store: znaki spoza ASCII jako \\uXXXX (np. kolory motd), znaki nowej linii ucieczką. */
    static String escape(String value) {
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            if (c == '\\') {
                sb.append("\\\\");
            } else if (c == '\n') {
                sb.append("\\n");
            } else if (c == '\r') {
                sb.append("\\r");
            } else if (c == ' ' && i == 0) {
                sb.append("\\ ");
            } else if (c < 0x20 || c > 0x7e) {
                sb.append(String.format("\\u%04X", (int) c));
            } else {
                sb.append(c);
            }
        }
        return sb.toString();
    }
}
