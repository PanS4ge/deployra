package io.deployra.minecraft;

import java.io.File;
import java.io.IOException;
import java.nio.file.FileVisitResult;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.SimpleFileVisitor;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** Pliki serwera ograniczone do jego katalogu (ścieżki kanoniczne, kasowanie bez podążania za dowiązaniami). */
final class ServerFiles {
    static final long LIMIT = 8L * 1024 * 1024;
    private final Path root;

    ServerFiles(File root) throws IOException {
        this.root = root.getCanonicalFile().toPath();
    }

    /** Ścieżka z panelu („/plugins/x.yml”) → plik wewnątrz katalogu serwera; wszystko poza nim jest odrzucane. */
    Path resolve(String path) throws IOException {
        String clean = path == null ? "" : path.replace('\\', '/');
        while (clean.startsWith("/")) {
            clean = clean.substring(1);
        }
        Path target = root.resolve(clean).normalize();
        Path real = target.toFile().getCanonicalFile().toPath();  // dowiązania prowadzące poza serwer też odpadają
        if (!real.equals(root) && !real.startsWith(root)) {
            throw new SecurityException("path outside the server directory");
        }
        return target;
    }

    List<Map<String, Object>> list(String path) throws IOException {
        Path dir = resolve(path);
        if (!Files.isDirectory(dir)) {
            throw new IOException("not a directory: " + path);
        }
        List<Map<String, Object>> out = new ArrayList<Map<String, Object>>();
        File[] children = dir.toFile().listFiles();
        if (children == null) {
            return out;
        }
        for (File f : children) {
            Map<String, Object> item = new LinkedHashMap<String, Object>();
            item.put("name", f.getName());
            item.put("is_dir", f.isDirectory());
            item.put("size", f.isFile() ? f.length() : 0L);
            out.add(item);
        }
        Collections.sort(out, new Comparator<Map<String, Object>>() {
            @Override
            public int compare(Map<String, Object> a, Map<String, Object> b) {
                boolean da = Boolean.TRUE.equals(a.get("is_dir")), db = Boolean.TRUE.equals(b.get("is_dir"));
                if (da != db) {
                    return da ? -1 : 1;
                }
                return String.valueOf(a.get("name")).compareToIgnoreCase(String.valueOf(b.get("name")));
            }
        });
        return out;
    }

    String read(String path) throws IOException {
        Path file = resolve(path);
        if (!Files.isRegularFile(file)) {
            throw new IOException("not a file: " + path);
        }
        if (Files.size(file) > LIMIT) {
            throw new IOException("file larger than 8 MB");
        }
        return Base64.getEncoder().encodeToString(Files.readAllBytes(file));
    }

    void write(String path, String dataB64) throws IOException {
        byte[] data = Base64.getDecoder().decode(dataB64 == null ? "" : dataB64);
        if (data.length > LIMIT) {
            throw new IOException("file larger than 8 MB");
        }
        Path file = resolve(path);
        if (file.equals(root) || Files.isDirectory(file)) {
            throw new IOException("not a file: " + path);
        }
        Files.createDirectories(file.getParent());
        Path tmp = file.resolveSibling(file.getFileName() + ".deployra-tmp");
        Files.write(tmp, data);
        Files.move(tmp, file, StandardCopyOption.REPLACE_EXISTING);
    }

    void mkdir(String path) throws IOException {
        Files.createDirectories(resolve(path));
    }

    void delete(String path) throws IOException {
        Path target = resolve(path);
        if (target.equals(root)) {
            throw new IOException("refusing to delete the server directory");
        }
        if (Files.isSymbolicLink(target) || !Files.isDirectory(target, LinkOption.NOFOLLOW_LINKS)) {
            Files.delete(target);
            return;
        }
        Files.walkFileTree(target, new SimpleFileVisitor<Path>() {  // bez FOLLOW_LINKS: dowiązanie usuwamy, celu nie ruszamy
            @Override
            public FileVisitResult visitFile(Path file, BasicFileAttributes attrs) throws IOException {
                Files.delete(file);
                return FileVisitResult.CONTINUE;
            }

            @Override
            public FileVisitResult postVisitDirectory(Path dir, IOException exc) throws IOException {
                Files.delete(dir);
                return FileVisitResult.CONTINUE;
            }
        });
    }
}
