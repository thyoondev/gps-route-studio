package local.gpsroute.bridge;

import android.app.*;
import android.content.Intent;
import android.location.*;
import android.os.*;
import org.json.JSONObject;
import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;

/** USB-forwarded, authenticated location test service. No LAN listener or storage. */
public final class BridgeService extends Service {
    private static final String[] PROVIDERS = {"gps", "network"};
    private final List<String> installed = new ArrayList<>();
    private final Handler main = new Handler(Looper.getMainLooper());
    private volatile boolean stopping;
    private volatile Location received;
    private volatile ServerSocket server;
    private volatile Socket client;
    private LocationManager locations;
    private Thread worker;
    private String sessionToken;
    private final LocationListener listener = fix -> received = new Location(fix);

    @Override public void onCreate() {
        super.onCreate();
        locations = getSystemService(LocationManager.class);
        installed.addAll(getSharedPreferences("bridge", 0).getStringSet("providers", Collections.emptySet()));
        NotificationManager notifications = getSystemService(NotificationManager.class);
        notifications.createNotificationChannel(new NotificationChannel("route", "GPS 경로 시험", NotificationManager.IMPORTANCE_LOW));
        Intent stop = new Intent(this, BridgeService.class).setAction("stop");
        PendingIntent action = PendingIntent.getService(this, 0, stop, PendingIntent.FLAG_IMMUTABLE);
        Notification notice = new Notification.Builder(this, "route")
                .setContentTitle("GPS Route Studio · 모의 위치 전송")
                .setContentText("Mac 연결이 끊기면 5초 안에 전송을 해제합니다.")
                .setSmallIcon(android.R.drawable.ic_menu_mylocation)
                .addAction(new Notification.Action.Builder(null, "중지 · GPS 복구", action).build())
                .setOngoing(true).build();
        startForeground(1, notice);
    }
    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) { stopSelf(); return START_NOT_STICKY; }
        if ("stop".equals(intent.getAction())) { shutdown(intent.getStringExtra("token")); return START_NOT_STICKY; }
        if (worker != null) return START_NOT_STICKY;
        try { restore(); }
        catch (RuntimeException error) { android.util.Log.e("GPSRouteBridge", "Previous session restore failed", error); stopSelf(); return START_NOT_STICKY; }
        final int port = intent.getIntExtra("port", 0);
        sessionToken = intent.getStringExtra("token");
        if (!"start".equals(intent.getAction()) || port < 1024 || port > 65535 ||
                sessionToken == null || !sessionToken.matches("[0-9a-f]{64}")) {
            stopSelf(); return START_NOT_STICKY;
        }
        worker = new Thread(() -> runBridge(port), "gps-route-bridge");
        worker.start();
        return START_NOT_STICKY;
    }
    private void runBridge(int port) {
        try {
            ServerSocket socket = new ServerSocket();
            server = socket;
            socket.setReuseAddress(true);
            socket.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), port));
            socket.setSoTimeout(15000);
            client = socket.accept(); client.setSoTimeout(5000);
            InputStream input = client.getInputStream();
            OutputStream output = client.getOutputStream();
            int previousSequence = 0;
            while (!stopping) {
                JSONObject message = new JSONObject(readLine(input));
                int sequence = message.getInt("sequence");
                if (!sessionToken.equals(message.optString("token")) || sequence <= previousSequence)
                    throw new IOException("Invalid session or sequence");
                previousSequence = sequence;
                String command = message.getString("command");
                JSONObject reply = new JSONObject().put("sequence", sequence);
                if ("hello".equals(command)) {
                    send(output, reply.put("applied", true));
                    continue;
                }
                if ("stop".equals(command)) {
                    stopping = true;
                    restore();
                    send(output, reply.put("restored", true));
                    return;
                }
                if (!"fix".equals(command)) throw new IOException("Unknown command");
                double lat = number(message, "lat", -90, 90);
                double lon = number(message, "lon", -180, 180);
                double speed = number(message, "speed", 0, 300);
                double bearing = number(message, "course", 0, 360);
                synchronized (this) {
                ensureProviders();
                for (String provider : PROVIDERS) {
                    Location fix = new Location(provider);
                    fix.setLatitude(lat); fix.setLongitude(lon); fix.setAltitude(30);
                    fix.setAccuracy(5); fix.setVerticalAccuracyMeters(5);
                    fix.setSpeed((float)(speed / 3.6)); fix.setSpeedAccuracyMetersPerSecond(0.2f);
                    fix.setBearing((float)bearing); fix.setBearingAccuracyDegrees(1);
                    fix.setTime(System.currentTimeMillis()); fix.setElapsedRealtimeNanos(SystemClock.elapsedRealtimeNanos());
                    locations.setTestProviderLocation(provider, fix);
                }
                }
                reply.put("applied", true).put("speed", speed);
                Location observed = received;
                if (observed != null) {
                    double age = (SystemClock.elapsedRealtimeNanos() - observed.getElapsedRealtimeNanos()) / 1e9;
                    if (age >= 0 && age < 3) {
                        reply.put("receivedSpeed", observed.hasSpeed() ? observed.getSpeed() * 3.6 : JSONObject.NULL);
                        reply.put("receivedMock", observed.isMock()).put("receivedAge", age);
                    }
                }
                send(output, reply);
            }
        } catch (Exception error) {
            android.util.Log.e("GPSRouteBridge", "Session ended: " + error.getClass().getSimpleName());
        } finally {
            try { restore(); }
            catch (RuntimeException error) { android.util.Log.e("GPSRouteBridge", "Restoration requires attention", error); }
            closeSockets();
            main.post(this::stopSelf);
        }
    }
    private synchronized void ensureProviders() {
        if (stopping) throw new IllegalStateException("Service stopping");
        if (!installed.isEmpty()) return;
        for (String provider : PROVIDERS) {
            installed.add(provider);
            saveProviders();
            locations.addTestProvider(provider, false, false, false, false, true, true, true,
                    Criteria.POWER_LOW, Criteria.ACCURACY_FINE);
            locations.setTestProviderEnabled(provider, true);
            locations.requestLocationUpdates(provider, 0, 0, listener, Looper.getMainLooper());
        }
    }
    private synchronized void restore() {
        locations.removeUpdates(listener);
        RuntimeException failure = null;
        for (String provider : new ArrayList<>(installed)) {
            try { locations.removeTestProvider(provider); installed.remove(provider); saveProviders(); }
            catch (RuntimeException error) { failure = error; }
        }
        if (failure != null) throw failure;
    }
    private void saveProviders() {
        if (!getSharedPreferences("bridge", 0).edit().putStringSet("providers", new HashSet<>(installed)).commit())
            throw new IllegalStateException("Could not save restoration state");
    }
    private void shutdown(String token) {
        stopping = true; closeSockets();
        try { restore(); reportRestore(token, null); }
        catch (RuntimeException error) { reportRestore(token, error); }
        finally { stopSelf(); }
    }
    private void reportRestore(String token, Exception failure) {
        android.util.AtomicFile file = new android.util.AtomicFile(new File(getFilesDir(), "restore.json"));
        FileOutputStream output = null;
        try {
            JSONObject report = new JSONObject().put("token", token == null ? "" : token)
                    .put("restored", failure == null).put("error", failure == null ? "" : failure.getClass().getSimpleName());
            output = file.startWrite();
            output.write(report.toString().getBytes(StandardCharsets.UTF_8));
            file.finishWrite(output);
        } catch (Exception error) {
            if (output != null) file.failWrite(output);
            android.util.Log.e("GPSRouteBridge", "Could not report restoration", error);
        }
    }
    private void closeSockets() {
        try { if (client != null) client.close(); } catch (IOException ignored) { }
        try { if (server != null) server.close(); } catch (IOException ignored) { }
    }
    @Override public void onDestroy() {
        stopping = true; closeSockets();
        try { restore(); }
        catch (RuntimeException error) { android.util.Log.e("GPSRouteBridge", "Restore failed during shutdown", error); }
        super.onDestroy();
    }
    @Override public IBinder onBind(Intent intent) { return null; }
    private static double number(JSONObject object, String key, double min, double max) throws Exception {
        double value = object.getDouble(key);
        if (!Double.isFinite(value) || value < min || value > max) throw new IOException("Invalid " + key);
        return value;
    }
    private static String readLine(InputStream input) throws IOException {
        ByteArrayOutputStream buffer = new ByteArrayOutputStream();
        long deadline = SystemClock.elapsedRealtime() + 5000;
        while (buffer.size() < 16384 && SystemClock.elapsedRealtime() < deadline) {
            int value = input.read();
            if (value < 0) throw new EOFException();
            if (value == '\n') return buffer.toString("UTF-8");
            buffer.write(value);
        }
        throw new IOException("Command timeout or too large");
    }
    private static void send(OutputStream output, JSONObject value) throws IOException {
        output.write((value.toString() + "\n").getBytes(StandardCharsets.UTF_8)); output.flush();
    }
}
