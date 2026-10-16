package io.deployra.minecraft;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.*;

/**
 * File management bridge for browsing, reading, writing, and managing server files safely.
 */
public class FilesManagerBridge {

    private static final File ROOT_DIR = new File(".").getAbsoluteFile();
    private static final SimpleDateFormat DATE_FORMAT = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss");

    private static File resolveSafe(String relativePath) throws IOException {
        if (relativePath == null || relativePath.trim().isEmpty() || relativePath.equals("/")) {
            return ROOT_DIR;
        }
        // Normalize
        String clean = relativePath.replace('\\', '/').trim();
        while (clean.startsWith("/")) {
            clean = clean.substring(1);
        }
        File target = new File(ROOT_DIR, clean).getCanonicalFile();
        String rootPath = ROOT_DIR.getCanonicalPath();
        if (!target.getCanonicalPath().startsWith(rootPath)) {
            throw new SecurityException("Access denied: path escapes server directory.");
        }
        return target;
    }

    public static List<Map<String, Object>> listDirectory(String relativePath) throws IOException {
        File dir = resolveSafe(relativePath);
        if (!dir.exists() || !dir.isDirectory()) {
            throw new FileNotFoundException("Directory not found: " + relativePath);
        }

        File[] files = dir.listFiles();
        List<Map<String, Object>> items = new ArrayList<>();
        if (files != null) {
            for (File f : files) {
                Map<String, Object> item = new LinkedHashMap<>();
                item.put("name", f.getName());
                item.put("is_dir", f.isDirectory());
                item.put("size", f.isDirectory() ? 0 : f.length());
                item.put("modified", DATE_FORMAT.format(new Date(f.lastModified())));
                
                String rel = getRelativePath(f);
                item.put("path", rel.isEmpty() ? "/" : rel);
                items.add(item);
            }
        }

        // Sort folders first, then alphabetical
        items.sort((a, b) -> {
            boolean aDir = (boolean) a.get("is_dir");
            boolean bDir = (boolean) b.get("is_dir");
            if (aDir != bDir) return aDir ? -1 : 1;
            return ((String) a.get("name")).compareToIgnoreCase((String) b.get("name"));
        });

        return items;
    }

    public static String readFileText(String relativePath, int maxBytes) throws IOException {
        File file = resolveSafe(relativePath);
        if (!file.exists() || !file.isFile()) {
            throw new FileNotFoundException("File not found: " + relativePath);
        }
        long len = file.length();
        if (maxBytes > 0 && len > maxBytes) {
            throw new IOException("File too large (" + (len / 1024) + " KB). Maximum allowed: " + (maxBytes / 1024) + " KB");
        }

        ByteArrayOutputStream baos = new ByteArrayOutputStream();
        try (InputStream is = new FileInputStream(file)) {
            byte[] buf = new byte[4096];
            int n;
            while ((n = is.read(buf)) != -1) {
                baos.write(buf, 0, n);
            }
        }
        return baos.toString(StandardCharsets.UTF_8.name());
    }

    public static boolean writeFileText(String relativePath, String content) throws IOException {
        File file = resolveSafe(relativePath);
        File parent = file.getParentFile();
        if (parent != null && !parent.exists()) {
            parent.mkdirs();
        }
        try (OutputStream os = new FileOutputStream(file)) {
            os.write(content.getBytes(StandardCharsets.UTF_8));
            return true;
        }
    }

    public static boolean deleteFileOrDir(String relativePath) throws IOException {
        File file = resolveSafe(relativePath);
        if (!file.exists()) return false;
        return deleteRecursively(file);
    }

    public static boolean createDirectory(String relativePath) throws IOException {
        File dir = resolveSafe(relativePath);
        return dir.mkdirs() || dir.isDirectory();
    }

    private static boolean deleteRecursively(File file) {
        if (file.isDirectory()) {
            File[] children = file.listFiles();
            if (children != null) {
                for (File c : children) {
                    deleteRecursively(c);
                }
            }
        }
        return file.delete();
    }

    private static String getRelativePath(File file) throws IOException {
        String rootPath = ROOT_DIR.getCanonicalPath();
        String filePath = file.getCanonicalPath();
        if (filePath.equals(rootPath)) return "";
        if (filePath.startsWith(rootPath)) {
            String rel = filePath.substring(rootPath.length()).replace('\\', '/');
            if (rel.startsWith("/")) rel = rel.substring(1);
            return rel;
        }
        return file.getName();
    }
}
