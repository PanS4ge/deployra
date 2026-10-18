package io.deployra.minecraft;

import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.RandomAccessFile;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.FileVisitResult;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.SimpleFileVisitor;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Collections;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;

/** Pliki serwera ograniczone do jego katalogu (ścieżki kanoniczne, kasowanie bez podążania za dowiązaniami). */
final class ServerFiles {
    static final long LIMIT = 8L * 1024 * 1024;
    /** Kawałek dużego pliku (wgrywanie i pobieranie bez limitu 8 MB, przez ten sam kanał co polecenia). */
    static final int CHUNK = 4 * 1024 * 1024;
    private static final Pattern UPLOAD_ID = Pattern.compile("^[a-f0-9]{16,32}$");
    private static final Pattern PART = Pattern.compile("^\\..+\\.deployra-[a-f0-9]{16,32}\\.part$");
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
            if (PART.matcher(f.getName()).matches()) {
                continue;  // niedokończone wgrywanie — nie pokazujemy go w panelu
            }
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

    /** Plik docelowy wgrywania: wewnątrz serwera, nie katalog. */
    private Path targetFile(String path) throws IOException {
        Path file = resolve(path);
        if (file.equals(root) || Files.isDirectory(file)) {
            throw new IOException("not a file: " + path);
        }
        return file;
    }

    private static Path part(Path file, String upload) throws IOException {
        if (upload == null || !UPLOAD_ID.matcher(upload).matches()) {
            throw new IOException("bad upload id");
        }
        return file.resolveSibling("." + file.getFileName() + ".deployra-" + upload + ".part");
    }

    /** Kolejny kawałek dużego pliku: dopisywany do pliku tymczasowego obok celu; cel podmienia dopiero commit. */
    long writeChunk(String path, String upload, long offset, String dataB64) throws IOException {
        byte[] data = Base64.getDecoder().decode(dataB64 == null ? "" : dataB64);
        if (data.length > CHUNK) {
            throw new IOException("chunk larger than 4 MB");
        }
        Path file = targetFile(path);
        Files.createDirectories(file.getParent());
        Path part = part(file, upload);
        if (offset == 0) {
            Files.deleteIfExists(part);  // początek wgrywania (także ponowionego)
        }
        long size = Files.exists(part) ? Files.size(part) : 0L;
        if (size != offset) {
            throw new IOException("chunk out of order: expected offset " + size);
        }
        OutputStream out = Files.newOutputStream(part, StandardOpenOption.CREATE, StandardOpenOption.APPEND);
        try {
            out.write(data);
        } finally {
            out.close();
        }
        return size + data.length;
    }

    /** Koniec wgrywania: rozmiar i SHA-256 muszą się zgadzać, potem atomowa podmiana pliku docelowego. */
    void commit(String path, String upload, long size, String sha256) throws IOException {
        Path file = targetFile(path);
        Path part = part(file, upload);
        if (!Files.isRegularFile(part)) {
            throw new IOException("upload not started: " + path);
        }
        long got = Files.size(part);
        if (got != size || (sha256 != null && !sha256.isEmpty() && !sha256.equalsIgnoreCase(sha256(part)))) {
            Files.deleteIfExists(part);
            throw new IOException(got != size ? "upload incomplete: " + got + " of " + size + " bytes" : "checksum mismatch");
        }
        try {
            Files.move(part, file, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
        } catch (AtomicMoveNotSupportedException e) {
            Files.move(part, file, StandardCopyOption.REPLACE_EXISTING);
        }
    }

    void abort(String path, String upload) throws IOException {
        Files.deleteIfExists(part(targetFile(path), upload));
    }

    /** Fragment pliku dowolnej wielkości: {data_b64, size} — pobieranie dużych plików kawałkami. */
    Map<String, Object> readChunk(String path, long offset, long length) throws IOException {
        Path file = resolve(path);
        if (!Files.isRegularFile(file)) {
            throw new IOException("not a file: " + path);
        }
        long size = Files.size(file);
        int len = (int) Math.max(0L, Math.min(Math.min(length, (long) CHUNK), size - Math.max(0L, offset)));
        byte[] buffer = new byte[len];
        RandomAccessFile raf = new RandomAccessFile(file.toFile(), "r");
        try {
            raf.seek(Math.max(0L, offset));
            raf.readFully(buffer);
        } finally {
            raf.close();
        }
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        out.put("data_b64", Base64.getEncoder().encodeToString(buffer));
        out.put("size", size);
        return out;
    }

    private static String sha256(Path file) throws IOException {
        MessageDigest digest;
        try {
            digest = MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException e) {
            throw new IOException(e);
        }
        InputStream in = Files.newInputStream(file);
        try {
            byte[] buffer = new byte[1 << 16];
            int n;
            while ((n = in.read(buffer)) > 0) {
                digest.update(buffer, 0, n);
            }
        } finally {
            in.close();
        }
        StringBuilder hex = new StringBuilder();
        for (byte b : digest.digest()) {
            hex.append(String.format("%02x", b));
        }
        return hex.toString();
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
