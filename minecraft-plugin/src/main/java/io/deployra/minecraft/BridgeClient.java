package io.deployra.minecraft;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.logging.Level;

/**
 * Połączenie z panelem: plugin sam pyta panel o polecenia (długie zapytanie), więc serwer nie otwiera żadnego portu.
 * Każde zapytanie jest podpisane, a odpowiedź panelu sprawdzana — bez poprawnego podpisu polecenie nie zostanie wykonane.
 */
final class BridgeClient implements Runnable {
    static final String POLL = "/minecraft/bridge/poll";
    static final String PAIR = "/minecraft/bridge/pair";

    private final DeployraPlugin plugin;
    private final Operations operations;
    private volatile boolean running = true;
    private volatile Map<String, Object> info;
    private volatile String lastError = "";
    private volatile long lastContact;
    private Thread thread;

    BridgeClient(DeployraPlugin plugin, Operations operations) {
        this.plugin = plugin;
        this.operations = operations;
    }

    void start() {
        thread = new Thread(this, "DeployraBridge");
        thread.setDaemon(true);
        thread.start();
    }

    void stop() {
        running = false;
        if (thread != null) {
            thread.interrupt();
        }
    }

    void setInfo(Map<String, Object> info) {
        this.info = info;
    }

    String status() {
        if (!plugin.paired()) {
            return "not paired";
        }
        long ago = (System.currentTimeMillis() - lastContact) / 1000L;
        return lastContact == 0 ? "connecting… " + lastError : (ago < 60 ? "connected" : "last contact " + ago + " s ago " + lastError);
    }

    @Override
    public void run() {
        List<Map<String, Object>> results = new ArrayList<Map<String, Object>>();
        long backoff = 2000;
        long infoSent = 0;
        while (running) {
            if (!plugin.paired()) {
                sleep(3000);
                continue;
            }
            try {
                Map<String, Object> payload = new LinkedHashMap<String, Object>();
                payload.put("results", results);
                if (info != null && System.currentTimeMillis() - infoSent > 30000) {
                    payload.put("info", info);
                    infoSent = System.currentTimeMillis();
                }
                List<Object> commands = poll(payload);
                results = new ArrayList<Map<String, Object>>();
                lastContact = System.currentTimeMillis();
                lastError = "";
                backoff = 2000;
                for (Object raw : commands) {
                    results.add(execute(Json.object(raw)));
                }
            } catch (Exception e) {
                lastError = e.getClass().getSimpleName() + ": " + e.getMessage();
                plugin.getLogger().log(Level.FINE, "Deployra: " + lastError);
                sleep(backoff);
                backoff = Math.min(60000, backoff * 2);
            }
        }
    }

    private Map<String, Object> execute(Map<String, Object> command) {
        Map<String, Object> result = new LinkedHashMap<String, Object>();
        result.put("id", Json.str(command.get("id"), ""));
        try {
            result.put("data", operations.run(Json.str(command.get("op"), ""), Json.object(command.get("args"))));
            result.put("ok", true);
        } catch (Throwable e) {
            Throwable cause = e.getCause() != null && e.getCause() != e ? e.getCause() : e;
            result.put("ok", false);
            result.put("error", cause.getClass().getSimpleName() + ": " + cause.getMessage());
        }
        return result;
    }

    private List<Object> poll(Map<String, Object> payload) throws IOException {
        byte[] body = Json.write(payload).getBytes(StandardCharsets.UTF_8);
        Signer signer = new Signer(plugin.secret(), String.valueOf(plugin.serverId()));
        HttpURLConnection conn = open(POLL, 40000);
        Map<String, String> signed = signer.sign("POST", POLL, body);
        for (Map.Entry<String, String> h : signed.entrySet()) {
            conn.setRequestProperty(h.getKey(), h.getValue());
        }
        send(conn, body);
        int code = conn.getResponseCode();
        byte[] answer = readAll(code >= 400 ? conn.getErrorStream() : conn.getInputStream());
        if (code == 401) {
            throw new IOException("panel rejected the signature (re-pair the server?)");
        }
        if (code != 200) {
            throw new IOException("HTTP " + code);
        }
        // odpowiedź jest podpisana razem z nonce TEGO żądania — nie da się podsunąć starej odpowiedzi
        String bound = POLL + "#" + signed.get(Signer.H_NONCE);
        if (!signer.verifyResponse(bound, answer, conn.getHeaderField(Signer.H_TS), conn.getHeaderField(Signer.H_NONCE), conn.getHeaderField(Signer.H_SIG))) {
            throw new IOException("bad response signature — ignoring commands");
        }
        return Json.list(Json.object(Json.parse(new String(answer, StandardCharsets.UTF_8))).get("commands"));
    }

    /** Wymiana jednorazowego kodu na sekret. Zwraca {server_id, secret}. */
    static Map<String, Object> pair(String panelUrl, String code, Map<String, Object> info) throws IOException {
        Map<String, Object> payload = new LinkedHashMap<String, Object>();
        payload.put("code", code);
        payload.put("info", info);
        HttpURLConnection conn = (HttpURLConnection) new URL(panelUrl.replaceAll("/+$", "") + PAIR).openConnection();
        conn.setConnectTimeout(10000);
        conn.setReadTimeout(20000);
        conn.setRequestMethod("POST");
        conn.setDoOutput(true);
        conn.setRequestProperty("Content-Type", "application/json");
        send(conn, Json.write(payload).getBytes(StandardCharsets.UTF_8));
        int status = conn.getResponseCode();
        byte[] answer = readAll(status >= 400 ? conn.getErrorStream() : conn.getInputStream());
        if (status != 200) {
            throw new IOException(status == 403 ? "invalid or expired pairing code" : "HTTP " + status);
        }
        return Json.object(Json.parse(new String(answer, StandardCharsets.UTF_8)));
    }

    private HttpURLConnection open(String path, int readTimeout) throws IOException {
        HttpURLConnection conn = (HttpURLConnection) new URL(plugin.panelUrl().replaceAll("/+$", "") + path).openConnection();
        conn.setConnectTimeout(10000);
        conn.setReadTimeout(readTimeout);
        conn.setRequestMethod("POST");
        conn.setDoOutput(true);
        conn.setInstanceFollowRedirects(false);
        conn.setRequestProperty("Content-Type", "application/json");
        conn.setRequestProperty("User-Agent", "DeployraBridge/" + plugin.getDescription().getVersion());
        return conn;
    }

    private static void send(HttpURLConnection conn, byte[] body) throws IOException {
        conn.setFixedLengthStreamingMode(body.length);
        OutputStream out = conn.getOutputStream();
        try {
            out.write(body);
        } finally {
            out.close();
        }
    }

    private static byte[] readAll(InputStream in) throws IOException {
        if (in == null) {
            return new byte[0];
        }
        try {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
                if (out.size() > 16 * 1024 * 1024) {
                    throw new IOException("response too large");
                }
            }
            return out.toByteArray();
        } finally {
            in.close();
        }
    }

    private void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
