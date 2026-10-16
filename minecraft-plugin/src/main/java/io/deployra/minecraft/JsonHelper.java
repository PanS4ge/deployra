package io.deployra.minecraft;

import java.util.*;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * Lightweight zero-dependency JSON serializer and parser for Deployra Minecraft Bridge.
 */
public class JsonHelper {

    public static String escape(String s) {
        if (s == null) return "";
        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char ch = s.charAt(i);
            switch (ch) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\b': sb.append("\\b"); break;
                case '\f': sb.append("\\f"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (ch < ' ') {
                        String t = "000" + Integer.toHexString(ch);
                        sb.append("\\u").append(t.substring(t.length() - 4));
                    } else {
                        sb.append(ch);
                    }
                    break;
            }
        }
        return sb.toString();
    }

    public static String toJson(Object obj) {
        if (obj == null) return "null";
        if (obj instanceof String) return "\"" + escape((String) obj) + "\"";
        if (obj instanceof Number || obj instanceof Boolean) return obj.toString();
        if (obj instanceof Map) {
            Map<?, ?> map = (Map<?, ?>) obj;
            StringBuilder sb = new StringBuilder("{");
            boolean first = true;
            for (Map.Entry<?, ?> entry : map.entrySet()) {
                if (!first) sb.append(",");
                sb.append("\"").append(escape(String.valueOf(entry.getKey()))).append("\":");
                sb.append(toJson(entry.getValue()));
                first = false;
            }
            sb.append("}");
            return sb.toString();
        }
        if (obj instanceof Collection) {
            Collection<?> col = (Collection<?>) obj;
            StringBuilder sb = new StringBuilder("[");
            boolean first = true;
            for (Object item : col) {
                if (!first) sb.append(",");
                sb.append(toJson(item));
                first = false;
            }
            sb.append("]");
            return sb.toString();
        }
        if (obj instanceof Object[]) {
            return toJson(Arrays.asList((Object[]) obj));
        }
        return "\"" + escape(obj.toString()) + "\"";
    }

    /**
     * Extracts simple string value for a key from a flat JSON payload like {"command":"say hi"}
     */
    public static String extractString(String json, String key) {
        if (json == null || key == null) return null;
        String pattern = "\"" + Pattern.quote(key) + "\"\\s*:\\s*\"([^\"\\\\]*(?:\\\\.[^\"\\\\]*)*)\"";
        Pattern p = Pattern.compile(pattern);
        Matcher m = p.matcher(json);
        if (m.find()) {
            return unescape(m.group(1));
        }
        return null;
    }

    /**
     * Extracts string array value for a key like {"placeholders":["%a%", "%b%"]}
     */
    public static List<String> extractStringList(String json, String key) {
        List<String> list = new ArrayList<>();
        if (json == null || key == null) return list;
        String pattern = "\"" + Pattern.quote(key) + "\"\\s*:\\s*\\[([^\\]]*)\\]";
        Pattern p = Pattern.compile(pattern);
        Matcher m = p.matcher(json);
        if (m.find()) {
            String arrayContent = m.group(1);
            Pattern itemPattern = Pattern.compile("\"([^\"\\\\]*(?:\\\\.[^\"\\\\]*)*)\"");
            Matcher itemMatcher = itemPattern.matcher(arrayContent);
            while (itemMatcher.find()) {
                list.add(unescape(itemMatcher.group(1)));
            }
        }
        return list;
    }

    /**
     * Extracts a key-value map of strings from {"properties":{"motd":"hello","difficulty":"easy"}}
     */
    public static Map<String, String> extractStringMap(String json, String key) {
        Map<String, String> map = new LinkedHashMap<>();
        if (json == null || key == null) return map;
        String pattern = "\"" + Pattern.quote(key) + "\"\\s*:\\s*\\{([^\\}]*)\\}";
        Pattern p = Pattern.compile(pattern);
        Matcher m = p.matcher(json);
        if (m.find()) {
            String objectContent = m.group(1);
            // Matches "key": "value" or "key": value
            Pattern pairPattern = Pattern.compile("\"([^\"]+)\"\\s*:\\s*(?:\"([^\"\\\\]*(?:\\\\.[^\"\\\\]*)*)\"|([^,}\\s]+))");
            Matcher pairMatcher = pairPattern.matcher(objectContent);
            while (pairMatcher.find()) {
                String k = pairMatcher.group(1);
                String v = pairMatcher.group(2) != null ? unescape(pairMatcher.group(2)) : pairMatcher.group(3);
                map.put(k, v);
            }
        }
        return map;
    }

    public static String unescape(String s) {
        if (s == null) return null;
        return s.replace("\\\"", "\"")
                .replace("\\\\", "\\")
                .replace("\\n", "\n")
                .replace("\\r", "\r")
                .replace("\\t", "\t")
                .replace("\\b", "\b")
                .replace("\\f", "\f");
    }
}
