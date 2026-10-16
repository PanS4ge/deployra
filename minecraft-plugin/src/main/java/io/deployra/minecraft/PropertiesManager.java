package io.deployra.minecraft;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.util.*;

/**
 * Manager for reading and updating server.properties safely.
 */
public class PropertiesManager {

    private static final File PROP_FILE = new File("server.properties");

    public static Map<String, String> getProperties() {
        Map<String, String> map = new LinkedHashMap<>();
        if (!PROP_FILE.exists() || !PROP_FILE.isFile()) {
            return map;
        }

        try (BufferedReader reader = new BufferedReader(new InputStreamReader(new FileInputStream(PROP_FILE), StandardCharsets.UTF_8))) {
            String line;
            while ((line = reader.readLine()) != null) {
                line = line.trim();
                if (line.isEmpty() || line.startsWith("#") || line.startsWith("!")) {
                    continue;
                }
                int eq = line.indexOf('=');
                if (eq > 0) {
                    String key = line.substring(0, eq).trim();
                    String val = line.substring(eq + 1).trim();
                    map.put(key, val);
                }
            }
        } catch (Exception e) {
            e.printStackTrace();
        }
        return map;
    }

    public static synchronized boolean updateProperties(Map<String, String> updates) {
        if (updates == null || updates.isEmpty()) return true;

        List<String> lines = new ArrayList<>();
        Set<String> updatedKeys = new HashSet<>();

        if (PROP_FILE.exists()) {
            try (BufferedReader reader = new BufferedReader(new InputStreamReader(new FileInputStream(PROP_FILE), StandardCharsets.UTF_8))) {
                String line;
                while ((line = reader.readLine()) != null) {
                    String trimmed = line.trim();
                    if (!trimmed.isEmpty() && !trimmed.startsWith("#") && !trimmed.startsWith("!")) {
                        int eq = trimmed.indexOf('=');
                        if (eq > 0) {
                            String key = trimmed.substring(0, eq).trim();
                            if (updates.containsKey(key)) {
                                lines.add(key + "=" + updates.get(key));
                                updatedKeys.add(key);
                                continue;
                            }
                        }
                    }
                    lines.add(line);
                }
            } catch (Exception e) {
                e.printStackTrace();
                return false;
            }
        }

        // Add any new keys that weren't present
        for (Map.Entry<String, String> entry : updates.entrySet()) {
            if (!updatedKeys.contains(entry.getKey())) {
                lines.add(entry.getKey() + "=" + entry.getValue());
            }
        }

        // Write back
        try (BufferedWriter writer = new BufferedWriter(new OutputStreamWriter(new FileOutputStream(PROP_FILE), StandardCharsets.UTF_8))) {
            for (String l : lines) {
                writer.write(l);
                writer.newLine();
            }
            return true;
        } catch (Exception e) {
            e.printStackTrace();
            return false;
        }
    }
}
