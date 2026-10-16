package io.deployra.minecraft;

import java.text.SimpleDateFormat;
import java.util.*;
import java.util.logging.Handler;
import java.util.logging.LogRecord;

/**
 * In-memory circular buffer for server console logs.
 */
public class LogCaptureHandler extends Handler {

    private final int maxLines;
    private final LinkedList<String> buffer = new LinkedList<>();
    private final SimpleDateFormat dateFormat = new SimpleDateFormat("HH:mm:ss");

    public LogCaptureHandler(int maxLines) {
        this.maxLines = maxLines > 0 ? maxLines : 150;
    }

    @Override
    public synchronized void publish(LogRecord record) {
        if (record == null || record.getMessage() == null) return;
        String time = dateFormat.format(new Date(record.getMillis()));
        String line = "[" + time + " " + record.getLevel().getName() + "] " + record.getMessage();
        buffer.add(line);
        while (buffer.size() > maxLines) {
            buffer.removeFirst();
        }
    }

    public synchronized List<String> getRecentLogs() {
        return new ArrayList<>(buffer);
    }

    @Override
    public void flush() {}

    @Override
    public void close() throws SecurityException {
        buffer.clear();
    }
}
