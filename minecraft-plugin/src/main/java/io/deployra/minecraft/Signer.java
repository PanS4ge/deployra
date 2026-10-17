package io.deployra.minecraft;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.Map;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * Podpisy HMAC-SHA256 zgodne z Deployrą (deployra_common.hmac_auth): metoda, ścieżka, czas, nonce, sha256 body.
 * Żądania pluginu podpisuje metoda "POST", odpowiedzi panelu — "RESPONSE"; powtórzony nonce jest odrzucany.
 */
final class Signer {
    static final String H_KEY = "X-Deployra-Key";
    static final String H_TS = "X-Deployra-Ts";
    static final String H_NONCE = "X-Deployra-Nonce";
    static final String H_SIG = "X-Deployra-Sig";
    static final long WINDOW = 300;

    private final byte[] secret;
    private final String keyId;
    private final SecureRandom random = new SecureRandom();
    private final Map<String, Long> seen = new LinkedHashMap<String, Long>();

    Signer(String secretHex, String keyId) {
        this.secret = hex(secretHex);
        this.keyId = keyId;
    }

    Map<String, String> sign(String method, String path, byte[] body) {
        long ts = System.currentTimeMillis() / 1000L;
        byte[] raw = new byte[12];
        random.nextBytes(raw);
        String nonce = toHex(raw);
        Map<String, String> headers = new LinkedHashMap<String, String>();
        headers.put(H_KEY, keyId);
        headers.put(H_TS, String.valueOf(ts));
        headers.put(H_NONCE, nonce);
        headers.put(H_SIG, toHex(mac(canonical(method, path, ts, nonce, body))));
        return headers;
    }

    /** Sprawdza podpis odpowiedzi panelu; fałsz = nie ufać treści. */
    synchronized boolean verifyResponse(String path, byte[] body, String tsRaw, String nonce, String sig) {
        if (tsRaw == null || nonce == null || sig == null) {
            return false;
        }
        long ts;
        try {
            ts = Long.parseLong(tsRaw);
        } catch (NumberFormatException e) {
            return false;
        }
        long now = System.currentTimeMillis() / 1000L;
        if (Math.abs(now - ts) > WINDOW) {
            return false;
        }
        byte[] expected = toHex(mac(canonical("RESPONSE", path, ts, nonce, body))).getBytes(StandardCharsets.US_ASCII);
        if (!MessageDigest.isEqual(expected, sig.getBytes(StandardCharsets.US_ASCII))) {
            return false;
        }
        Iterator<Map.Entry<String, Long>> it = seen.entrySet().iterator();
        while (it.hasNext() && (it.next().getValue() < now - 2 * WINDOW || seen.size() > 5000)) {
            it.remove();
        }
        if (seen.containsKey(nonce)) {
            return false;
        }
        seen.put(nonce, now);
        return true;
    }

    static byte[] canonical(String method, String path, long ts, String nonce, byte[] body) {
        String text = method.toUpperCase() + "\n" + path + "\n" + ts + "\n" + nonce + "\n" + toHex(sha256(body));
        return text.getBytes(StandardCharsets.UTF_8);
    }

    private byte[] mac(byte[] data) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(secret, "HmacSHA256"));
            return mac.doFinal(data);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    static byte[] sha256(byte[] data) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(data);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    static String toHex(byte[] data) {
        StringBuilder sb = new StringBuilder(data.length * 2);
        for (byte b : data) {
            sb.append(Character.forDigit((b >> 4) & 0xF, 16)).append(Character.forDigit(b & 0xF, 16));
        }
        return sb.toString();
    }

    static byte[] hex(String text) {
        if (text == null || text.length() % 2 != 0) {
            throw new IllegalArgumentException("bad secret");
        }
        byte[] out = new byte[text.length() / 2];
        for (int i = 0; i < out.length; i++) {
            out[i] = (byte) Integer.parseInt(text.substring(2 * i, 2 * i + 2), 16);
        }
        return out;
    }
}
