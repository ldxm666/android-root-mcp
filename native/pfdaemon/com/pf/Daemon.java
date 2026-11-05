// language: Java
// file: native/pfdaemon/com/pf/Daemon.java
// target: Android 16 (SDK 36), arm64, started via app_process
// build:  javac --release 8 -cp android.jar -d classes com/pf/Daemon.java
//         d8 --lib android.jar --min-api 30 --output out classes/com/pf/*.class
//
// Two gesture engines, picked at startup and reported in `ping`:
//
//   devnode  (strategy A, preferred) — pack struct input_event[] and hand the whole
//            burst to /dev/input/eventN in ONE write(2). ~24 bytes per event, no
//            binder, no dispatch round trip. Kernel stamps event time itself, so
//            eventTime fidelity is exact by construction. Requires root: the node is
//            crw-rw---- root:input and SELinux input_device denies even group write.
//
//   inject   (strategy B) — android.hardware.input.InputManagerGlobal.injectInputEvent
//            with WAIT_FOR_FINISH. Works as plain shell UID 2000 (INJECT_EVENTS),
//            used for multi-touch on ROMs where devnode is locked, and always for
//            key/text since the touchscreen node carries no keycodes.
//
// sendevent-per-event (strategy C) is NOT here — it measured 25ms/event and lives in
// fallback.py only so the feature has a last resort.

package com.pf;

import android.os.IBinder;
import android.os.SystemClock;
import android.view.InputDevice;
import android.view.KeyCharacterMap;
import android.view.KeyEvent;
import android.view.MotionEvent;

import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.io.PrintWriter;
import java.lang.reflect.Method;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Base64;
import java.util.List;
import java.util.concurrent.locks.ReentrantLock;

public final class Daemon {

    // ---- evdev constants (linux/input-event-codes.h) ----
    static final int EV_SYN = 0x00, EV_KEY = 0x01, EV_ABS = 0x03;
    static final int SYN_REPORT = 0x00;
    static final int ABS_MT_SLOT = 0x2f, ABS_MT_TOUCH_MAJOR = 0x30, ABS_MT_WIDTH_MAJOR = 0x32;
    static final int ABS_MT_POSITION_X = 0x35, ABS_MT_POSITION_Y = 0x36;
    static final int ABS_MT_TRACKING_ID = 0x39, ABS_MT_PRESSURE = 0x3a;
    static final int BTN_TOUCH = 0x14a, BTN_TOOL_FINGER = 0x145;

    // struct input_event on 64-bit: timeval{int64,int64} + u16 type + u16 code + s32 value
    static final int EV_SIZE = 24;

    static final int MODE_ASYNC = 0, MODE_WAIT_FOR_FINISH = 2;
    static final long STEP_MS = 8;
    static final long MAX_DUR_MS = 20000;

    // ---- device geometry, filled by probe() ----
    static volatile int dispW = 1080, dispH = 2400;
    static volatile int rawMaxX = 14399, rawMaxY = 31999;
    static volatile int rotation = 0;
    static volatile String devPath = null;
    static volatile String devName = "?";

    static volatile FileOutputStream devOut = null;

    // ---- inject reflection state ----
    static volatile Object injTarget = null;
    static volatile Method injMethod = null;
    static volatile Method setDisplayId = null;

    static volatile String route = "none";
    static final ReentrantLock LOCK = new ReentrantLock();
    static int trackingId = 1;

    public static void main(String[] args) {
        int port = 0;
        boolean selftest = false, noDev = false;
        String devForce = null;
        for (int i = 0; i < args.length; i++) {
            if ("--port".equals(args[i]) && i + 1 < args.length) port = Integer.parseInt(args[++i]);
            else if ("--selftest".equals(args[i])) selftest = true;
            else if ("--no-devnode".equals(args[i])) noDev = true;
            else if ("--devnode".equals(args[i]) && i + 1 < args.length) devForce = args[++i];
        }

        exemptHiddenApis();
        probeGeometry();
        readRotation();

        if (!noDev) openDevnode(devForce);
        try { bindInject(); } catch (Throwable t) { log("inject unavailable: " + t); }

        if (devOut != null) route = "devnode";
        else if (injMethod != null) route = "inject";
        else {
            System.out.println("{\"ok\":false,\"err\":\"no_engine\",\"detail\":\"neither devnode nor inject available\"}");
            System.out.flush();
            System.exit(3);
            return;
        }
        log("route=" + route + " devnode=" + devPath + " (" + devName + ") maxX=" + rawMaxX
                + " maxY=" + rawMaxY + " disp=" + dispW + "x" + dispH + " rot=" + rotation);

        // Belt and braces: never leave a finger down from a previous incarnation.
        if (devOut != null) { try { resetTouch(); } catch (Throwable ignored) { } }

        if (selftest) {
            System.out.println("{\"ok\":true,\"route\":\"" + esc(route) + "\",\"uid\":" + uid()
                    + ",\"sdk\":" + sdk() + ",\"devnode\":\"" + esc(String.valueOf(devPath))
                    + "\",\"w\":" + dispW + ",\"h\":" + dispH + "}");
            System.out.flush();
            System.exit(0);
            return;
        }
        if (port <= 0) { System.err.println("usage: ... com.pf.Daemon --port <N>"); System.exit(2); return; }

        ServerSocket server;
        try {
            server = new ServerSocket(port, 8, InetAddress.getByName("127.0.0.1"));
        } catch (Throwable t) {
            System.out.println("{\"ok\":false,\"err\":\"listen_failed\",\"detail\":\"" + esc(String.valueOf(t)) + "\"}");
            System.out.flush(); System.exit(4); return;
        }
        System.out.println("$READY " + port + " " + route);
        System.out.flush();
        log("listening 127.0.0.1:" + port);

        while (true) {
            final Socket client;
            try { client = server.accept(); }
            catch (Throwable t) { log("accept " + t); continue; }
            Thread th = new Thread(new Runnable() {
                public void run() {
                    try { serve(client); }
                    catch (Throwable t) { log("serve " + t); }
                    finally { try { client.close(); } catch (Throwable ignored) { } }
                }
            }, "pfconn");
            th.setDaemon(true);
            th.start();
        }
    }

    // ================================================================ protocol

    static void serve(Socket client) throws Exception {
        client.setTcpNoDelay(true);
        try { client.setSoTimeout(120000); } catch (Throwable ignored) { }
        BufferedReader in = new BufferedReader(new InputStreamReader(client.getInputStream(), StandardCharsets.UTF_8));
        OutputStream raw = client.getOutputStream();
        PrintWriter out = new PrintWriter(new java.io.OutputStreamWriter(raw, StandardCharsets.UTF_8));

        String line;
        while ((line = in.readLine()) != null) {
            line = line.trim();
            if (line.isEmpty()) continue;
            if ("quit".equals(line)) { out.print("{\"ok\":true}\n"); out.flush(); safeShutdown(); return; }
            if ("shot".equals(line)) { out.flush(); writeShot(raw); continue; }

            String reply;
            try { reply = dispatch(line); }
            catch (Throwable t) {
                // One malformed line must never take the daemon down.
                reply = "{\"ok\":false,\"err\":\"exception\",\"detail\":\"" + esc(String.valueOf(t)) + "\"}";
                try { safeRelease(); } catch (Throwable ignored) { }
            }
            out.print(reply); out.print('\n'); out.flush();
        }
    }

    static String dispatch(String line) {
        String[] p = line.split("\\s+");
        String cmd = p[0];
        long t0 = SystemClock.uptimeMillis();

        if ("ping".equals(cmd)) {
            return "{\"ok\":true,\"uid\":" + uid() + ",\"sdk\":" + sdk() + ",\"route\":\"" + route
                    + "\",\"w\":" + dispW + ",\"h\":" + dispH + ",\"rot\":" + rotation
                    + ",\"touch\":\"" + esc(String.valueOf(devPath)) + "\"}";
        }
        if ("rotation".equals(cmd)) {
            readRotation();
            return "{\"ok\":true,\"rot\":" + rotation + ",\"w\":" + dispW + ",\"h\":" + dispH + "}";
        }
        if ("tap".equals(cmd)) return finish(t0, tap(f(p[1]), f(p[2])));
        if ("long_press".equals(cmd)) {
            long ms = Long.parseLong(p[3]);
            if (ms < 1 || ms > MAX_DUR_MS) return err("duration_out_of_range");
            return finish(t0, longPress(f(p[1]), f(p[2]), ms));
        }
        if ("swipe".equals(cmd)) {
            long dur = Long.parseLong(p[5]);
            if (dur < 1 || dur > MAX_DUR_MS) return err("duration_out_of_range");
            List<float[]> pts = new ArrayList<float[]>();
            pts.add(new float[]{f(p[1]), f(p[2])});
            pts.add(new float[]{f(p[3]), f(p[4])});
            return finish(t0, path(pts, dur));
        }
        if ("gesture".equals(cmd)) {
            int open = line.indexOf('['), close = line.lastIndexOf(']');
            if (open < 0 || close <= open) return err("bad_gesture");
            List<float[]> pts = parsePoints(line.substring(open, close + 1));
            if (pts.size() < 2) return err("need_2_points");
            String tail = line.substring(close + 1).trim();
            long dur = tail.isEmpty() ? 300 : Long.parseLong(tail.split("\\s+")[0]);
            if (dur < 1 || dur > MAX_DUR_MS) return err("duration_out_of_range");
            return finish(t0, path(pts, dur));
        }
        if ("pinch".equals(cmd)) {
            long dur = Long.parseLong(p[5]);
            if (dur < 1 || dur > MAX_DUR_MS) return err("duration_out_of_range");
            return finish(t0, pinch(f(p[1]), f(p[2]), f(p[3]), f(p[4]), dur,
                    p.length > 6 ? f(p[6]) : 0f));
        }
        if ("key".equals(cmd)) return finish(t0, key(parseKey(p[1])));
        if ("text".equals(cmd)) {
            String rest = line.substring(4).trim();
            String force = null;
            if (rest.startsWith("-f ")) {
                String[] parts = rest.split("\\s+", 3);
                force = parts[1];
                rest = parts.length > 2 ? parts[2] : "";
            }
            return finish(t0, text(new String(Base64.getDecoder().decode(rest), StandardCharsets.UTF_8), force));
        }
        if ("__boom".equals(cmd)) {
            // Fault-injection hook: hold a finger down, then throw. Proves the
            // finally-block release actually fires.
            if (!LOCK.tryLock()) return err("busy");
            try {
                int[] r = toRaw(f(p[1]), f(p[2]));
                ByteArrayOutputStream b = new ByteArrayOutputStream();
                ev(b, EV_ABS, ABS_MT_SLOT, 0);
                ev(b, EV_ABS, ABS_MT_TRACKING_ID, nextId());
                ev(b, EV_ABS, ABS_MT_POSITION_X, r[0]);
                ev(b, EV_ABS, ABS_MT_POSITION_Y, r[1]);
                ev(b, EV_KEY, BTN_TOUCH, 1);
                ev(b, EV_KEY, BTN_TOOL_FINGER, 1);
                ev(b, EV_SYN, SYN_REPORT, 0);
                try { writeBurst(b); } catch (Exception ex) { throw new RuntimeException(ex); }
                throw new IllegalStateException("deliberate fault after DOWN");
            } finally {
                try { safeRelease(); } catch (Throwable ignored) { }
                if (LOCK.isHeldByCurrentThread()) LOCK.unlock();
            }
        }
        return err("unknown_cmd:" + cmd);
    }

    static String finish(long t0, String body) {
        long ms = SystemClock.uptimeMillis() - t0;
        if (body.endsWith("}")) body = body.substring(0, body.length() - 1);
        return body + ",\"route\":\"" + route + "\",\"raw_ms\":" + ms + "}";
    }

    static float f(String s) { return Float.parseFloat(s); }

    static int parseKey(String s) {
        if (s.matches("-?\\d+")) return Integer.parseInt(s);
        try {
            java.lang.reflect.Field fl = KeyEvent.class.getField(s.startsWith("KEYCODE_") ? s : "KEYCODE_" + s);
            return fl.getInt(null);
        } catch (Throwable t) { throw new IllegalArgumentException("bad key " + s); }
    }

    static List<float[]> parsePoints(String s) {
        List<float[]> out = new ArrayList<float[]>();
        String t = s.replace('[', ' ').replace(']', ' ');
        String[] nums = t.trim().split("[,\\s]+");
        List<Float> v = new ArrayList<Float>();
        for (String n : nums) if (!n.isEmpty()) v.add(Float.parseFloat(n));
        for (int i = 0; i + 1 < v.size(); i += 2) out.add(new float[]{v.get(i), v.get(i + 1)});
        return out;
    }

    // ============================================================ devnode engine

    static int nextId() {
        trackingId++;
        if (trackingId > 60000) trackingId = 1;
        return trackingId;
    }

    static void ev(ByteArrayOutputStream b, int type, int code, int value) {
        byte[] e = new byte[EV_SIZE];
        // bytes 0..15 are the timeval; evdev_write() discards it and the input core
        // stamps the event at inject time, so we deliberately leave it zero.
        e[16] = (byte) (type & 0xff);
        e[17] = (byte) ((type >> 8) & 0xff);
        e[18] = (byte) (code & 0xff);
        e[19] = (byte) ((code >> 8) & 0xff);
        e[20] = (byte) (value & 0xff);
        e[21] = (byte) ((value >> 8) & 0xff);
        e[22] = (byte) ((value >> 16) & 0xff);
        e[23] = (byte) ((value >> 24) & 0xff);
        b.write(e, 0, EV_SIZE);
    }

    static void writeBurst(ByteArrayOutputStream b) throws Exception {
        if (devOut == null) throw new IllegalStateException("devnode closed");
        devOut.write(b.toByteArray());   // single write(2) for the whole burst
        devOut.flush();
    }

    /** Release every slot and clear the button state. Idempotent, always safe. */
    static void resetTouch() throws Exception {
        if (devOut == null) return;
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        for (int slot = 0; slot < 10; slot++) {
            ev(b, EV_ABS, ABS_MT_SLOT, slot);
            ev(b, EV_ABS, ABS_MT_TRACKING_ID, -1);
        }
        ev(b, EV_ABS, ABS_MT_SLOT, 0);
        ev(b, EV_KEY, BTN_TOUCH, 0);
        ev(b, EV_KEY, BTN_TOOL_FINGER, 0);
        ev(b, EV_SYN, SYN_REPORT, 0);
        writeBurst(b);
    }

    static void safeRelease() {
        try { resetTouch(); } catch (Throwable ignored) { }
    }

    static void safeShutdown() {
        safeRelease();
        try { if (devOut != null) devOut.close(); } catch (Throwable ignored) { }
        System.exit(0);
    }

    /** logical display coords -> panel raw coords, honouring rotation. */
    static int[] toRaw(float x, float y) {
        float px = x, py = y;
        switch (rotation) {
            case 1: px = y; py = dispH - x; break;
            case 2: px = dispW - x; py = dispH - y; break;
            case 3: px = dispW - y; py = x; break;
            default: break;
        }
        int rx = Math.round(px * rawMaxX / (float) Math.max(1, dispW - 1));
        int ry = Math.round(py * rawMaxY / (float) Math.max(1, dispH - 1));
        if (rx < 0) rx = 0; if (rx > rawMaxX) rx = rawMaxX;
        if (ry < 0) ry = 0; if (ry > rawMaxY) ry = rawMaxY;
        return new int[]{rx, ry};
    }

    // ============================================================== inject engine

    static void injectMode(android.view.InputEvent e, int mode) throws Exception {
        if (setDisplayId != null && e instanceof MotionEvent) {
            try { setDisplayId.invoke(e, 0); } catch (Throwable ignored) { }
        }
        Object r = injMethod.invoke(injTarget, e, mode);
        if (mode != MODE_ASYNC && r instanceof Boolean && !((Boolean) r))
            throw new IllegalStateException("injectInputEvent returned false");
    }

    static void inject(android.view.InputEvent e) throws Exception { injectMode(e, MODE_WAIT_FOR_FINISH); }

    static MotionEvent motion(long down, long when, int action, int[] ids, float[] xs, float[] ys, float[] pr) {
        int n = ids.length;
        MotionEvent.PointerProperties[] pp = new MotionEvent.PointerProperties[n];
        MotionEvent.PointerCoords[] pc = new MotionEvent.PointerCoords[n];
        for (int i = 0; i < n; i++) {
            pp[i] = new MotionEvent.PointerProperties();
            pp[i].id = ids[i];
            pp[i].toolType = MotionEvent.TOOL_TYPE_FINGER;
            pc[i] = new MotionEvent.PointerCoords();
            pc[i].x = xs[i]; pc[i].y = ys[i];
            pc[i].pressure = pr == null ? 1f : pr[i];
            pc[i].size = 1f;
        }
        return MotionEvent.obtain(down, when, action, n, pp, pc, 0, 0, 1f, 1f,
                KeyCharacterMap.VIRTUAL_KEYBOARD, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
    }

    // ================================================================= gestures

    static String tap(float x, float y) {
        if (!LOCK.tryLock()) return busy();
        try {
            byte[] down, up;
            if (devOut != null) {
                int[] r = toRaw(x, y);
                int id = nextId();
                ByteArrayOutputStream b = new ByteArrayOutputStream();
                ev(b, EV_ABS, ABS_MT_SLOT, 0);
                ev(b, EV_ABS, ABS_MT_TRACKING_ID, id);
                ev(b, EV_ABS, ABS_MT_POSITION_X, r[0]);
                ev(b, EV_ABS, ABS_MT_POSITION_Y, r[1]);
                ev(b, EV_ABS, ABS_MT_TOUCH_MAJOR, 6);
                ev(b, EV_ABS, ABS_MT_PRESSURE, 128);
                ev(b, EV_KEY, BTN_TOUCH, 1);
                ev(b, EV_KEY, BTN_TOOL_FINGER, 1);
                ev(b, EV_SYN, SYN_REPORT, 0);
                down = b.toByteArray();

                ByteArrayOutputStream u = new ByteArrayOutputStream();
                ev(u, EV_ABS, ABS_MT_SLOT, 0);
                ev(u, EV_ABS, ABS_MT_TRACKING_ID, -1);
                ev(u, EV_KEY, BTN_TOUCH, 0);
                ev(u, EV_KEY, BTN_TOOL_FINGER, 0);
                ev(u, EV_SYN, SYN_REPORT, 0);
                up = u.toByteArray();
            } else { down = null; up = null; }

            if (devOut != null) {
                long t = SystemClock.uptimeMillis();
                devOut.write(down); devOut.flush();
                sleepUntil(t + 32);
                devOut.write(up); devOut.flush();
            } else {
                long d = SystemClock.uptimeMillis();
                inject(motion(d, d, MotionEvent.ACTION_DOWN, new int[]{0}, new float[]{x}, new float[]{y}, new float[]{1f}));
                sleepUntil(d + 32);
                long u = SystemClock.uptimeMillis();
                inject(motion(d, u, MotionEvent.ACTION_UP, new int[]{0}, new float[]{x}, new float[]{y}, new float[]{0f}));
            }
            return "{\"ok\":true,\"events\":8}";
        } catch (Throwable t) {
            return fail(t);
        } finally {
            try { safeRelease(); } catch (Throwable ignored) { }
            LOCK.unlock();
        }
    }

    static String longPress(float x, float y, long ms) {
        if (!LOCK.tryLock()) return busy();
        try {
            if (devOut != null) {
                int[] r = toRaw(x, y);
                ByteArrayOutputStream b = new ByteArrayOutputStream();
                ev(b, EV_ABS, ABS_MT_SLOT, 0);
                ev(b, EV_ABS, ABS_MT_TRACKING_ID, nextId());
                ev(b, EV_ABS, ABS_MT_POSITION_X, r[0]);
                ev(b, EV_ABS, ABS_MT_POSITION_Y, r[1]);
                ev(b, EV_ABS, ABS_MT_TOUCH_MAJOR, 6);
                ev(b, EV_KEY, BTN_TOUCH, 1);
                ev(b, EV_KEY, BTN_TOOL_FINGER, 1);
                ev(b, EV_SYN, SYN_REPORT, 0);
                devOut.write(b.toByteArray()); devOut.flush();

                // A perfectly static hold reads as synthetic; jitter one raw unit.
                long steps = Math.max(1, ms / 50);
                long down = SystemClock.uptimeMillis();
                for (long i = 1; i <= steps; i++) {
                    sleepUntil(down + (ms * i / steps));
                    ByteArrayOutputStream m = new ByteArrayOutputStream();
                    ev(m, EV_ABS, ABS_MT_POSITION_X, r[0] + (int) (i % 2 == 0 ? 1 : -1));
                    ev(m, EV_ABS, ABS_MT_POSITION_Y, r[1]);
                    ev(m, EV_SYN, SYN_REPORT, 0);
                    devOut.write(m.toByteArray()); devOut.flush();
                }
            } else {
                long d = SystemClock.uptimeMillis();
                inject(motion(d, d, MotionEvent.ACTION_DOWN, new int[]{0}, new float[]{x}, new float[]{y}, new float[]{1f}));
                long steps = Math.max(1, ms / 50);
                for (long i = 1; i <= steps; i++) {
                    sleepUntil(d + (ms * i / steps));
                    long t = SystemClock.uptimeMillis();
                    injectMode(motion(d, t, MotionEvent.ACTION_MOVE, new int[]{0},
                            new float[]{x + (i % 2 == 0 ? 0.4f : -0.4f)}, new float[]{y}, new float[]{1f}), MODE_ASYNC);
                }
            }
            return "{\"ok\":true}";
        } catch (Throwable t) {
            return fail(t);
        } finally {
            try { safeRelease(); } catch (Throwable ignored) { }
            LOCK.unlock();
        }
    }

    static String path(List<float[]> pts, long durMs) {
        if (!LOCK.tryLock()) return busy();
        try {
            if (durMs < STEP_MS) durMs = STEP_MS;
            int segs = pts.size();
            double[] cum = new double[segs];
            for (int i = 1; i < segs; i++) {
                double dx = pts.get(i)[0] - pts.get(i - 1)[0];
                double dy = pts.get(i)[1] - pts.get(i - 1)[1];
                cum[i] = cum[i - 1] + Math.hypot(dx, dy);
            }
            double total = cum[segs - 1];
            int steps = (int) Math.max(2, durMs / STEP_MS);
            long down = SystemClock.uptimeMillis();

            if (devOut != null) {
                int[] r0 = toRaw(pts.get(0)[0], pts.get(0)[1]);
                ByteArrayOutputStream b = new ByteArrayOutputStream();
                ev(b, EV_ABS, ABS_MT_SLOT, 0);
                ev(b, EV_ABS, ABS_MT_TRACKING_ID, nextId());
                ev(b, EV_ABS, ABS_MT_POSITION_X, r0[0]);
                ev(b, EV_ABS, ABS_MT_POSITION_Y, r0[1]);
                ev(b, EV_ABS, ABS_MT_TOUCH_MAJOR, 6);
                ev(b, EV_KEY, BTN_TOUCH, 1);
                ev(b, EV_KEY, BTN_TOOL_FINGER, 1);
                ev(b, EV_SYN, SYN_REPORT, 0);
                devOut.write(b.toByteArray()); devOut.flush();

                for (int s = 1; s <= steps; s++) {
                    sleepUntil(down + (durMs * s / steps));
                    float[] xy = pointAt(pts, cum, total * s / (double) steps);
                    int[] r = toRaw(xy[0], xy[1]);
                    ByteArrayOutputStream m = new ByteArrayOutputStream();
                    ev(m, EV_ABS, ABS_MT_POSITION_X, r[0]);
                    ev(m, EV_ABS, ABS_MT_POSITION_Y, r[1]);
                    ev(m, EV_SYN, SYN_REPORT, 0);
                    devOut.write(m.toByteArray()); devOut.flush();
                }
            } else {
                inject(motion(down, down, MotionEvent.ACTION_DOWN, new int[]{0},
                        new float[]{pts.get(0)[0]}, new float[]{pts.get(0)[1]}, new float[]{1f}));
                for (int s = 1; s <= steps; s++) {
                    sleepUntil(down + (durMs * s / steps));
                    float[] xy = pointAt(pts, cum, total * s / (double) steps);
                    long t = SystemClock.uptimeMillis();
                    injectMode(motion(down, t, MotionEvent.ACTION_MOVE, new int[]{0},
                            new float[]{xy[0]}, new float[]{xy[1]}, new float[]{1f}), MODE_ASYNC);
                }
            }
            return "{\"ok\":true,\"events\":" + (steps + 2) + "}";
        } catch (Throwable t) {
            return fail(t);
        } finally {
            try { safeRelease(); } catch (Throwable ignored) { }
            LOCK.unlock();
        }
    }

    static float[] pointAt(List<float[]> pts, double[] cum, double d) {
        if (d <= 0) return new float[]{pts.get(0)[0], pts.get(0)[1]};
        double total = cum[cum.length - 1];
        if (d >= total) { float[] l = pts.get(pts.size() - 1); return new float[]{l[0], l[1]}; }
        int i = 1;
        while (i < cum.length && cum[i] < d) i++;
        double segLen = cum[i] - cum[i - 1];
        double f = segLen <= 0 ? 0 : (d - cum[i - 1]) / segLen;
        float[] a = pts.get(i - 1), b = pts.get(i);
        return new float[]{(float) (a[0] + (b[0] - a[0]) * f), (float) (a[1] + (b[1] - a[1]) * f)};
    }

    static String pinch(float cx, float cy, float r1, float r2, long durMs, float rotDeg) {
        if (!LOCK.tryLock()) return busy();
        try {
            if (durMs < STEP_MS) durMs = STEP_MS;
            int steps = (int) Math.max(2, durMs / STEP_MS);
            double rot = Math.toRadians(rotDeg);
            long down = SystemClock.uptimeMillis();

            if (devOut != null) {
                int[] a0 = toRaw((float) (cx + r1 * Math.cos(rot + Math.PI)), (float) (cy + r1 * Math.sin(rot + Math.PI)));
                int[] b0 = toRaw((float) (cx + r1 * Math.cos(rot)), (float) (cy + r1 * Math.sin(rot)));
                ByteArrayOutputStream b = new ByteArrayOutputStream();
                ev(b, EV_ABS, ABS_MT_SLOT, 0);
                ev(b, EV_ABS, ABS_MT_TRACKING_ID, nextId());
                ev(b, EV_ABS, ABS_MT_POSITION_X, a0[0]);
                ev(b, EV_ABS, ABS_MT_POSITION_Y, a0[1]);
                ev(b, EV_ABS, ABS_MT_TOUCH_MAJOR, 6);
                ev(b, EV_ABS, ABS_MT_SLOT, 1);
                ev(b, EV_ABS, ABS_MT_TRACKING_ID, nextId());
                ev(b, EV_ABS, ABS_MT_POSITION_X, b0[0]);
                ev(b, EV_ABS, ABS_MT_POSITION_Y, b0[1]);
                ev(b, EV_ABS, ABS_MT_TOUCH_MAJOR, 6);
                ev(b, EV_KEY, BTN_TOUCH, 1);
                ev(b, EV_KEY, BTN_TOOL_FINGER, 1);
                ev(b, EV_SYN, SYN_REPORT, 0);
                devOut.write(b.toByteArray()); devOut.flush();

                for (int s = 1; s <= steps; s++) {
                    sleepUntil(down + (durMs * s / steps));
                    float r = r1 + (r2 - r1) * s / (float) steps;
                    int[] a = toRaw((float) (cx + r * Math.cos(rot + Math.PI)), (float) (cy + r * Math.sin(rot + Math.PI)));
                    int[] bb = toRaw((float) (cx + r * Math.cos(rot)), (float) (cy + r * Math.sin(rot)));
                    ByteArrayOutputStream m = new ByteArrayOutputStream();
                    ev(m, EV_ABS, ABS_MT_SLOT, 0);
                    ev(m, EV_ABS, ABS_MT_POSITION_X, a[0]);
                    ev(m, EV_ABS, ABS_MT_POSITION_Y, a[1]);
                    ev(m, EV_ABS, ABS_MT_SLOT, 1);
                    ev(m, EV_ABS, ABS_MT_POSITION_X, bb[0]);
                    ev(m, EV_ABS, ABS_MT_POSITION_Y, bb[1]);
                    ev(m, EV_SYN, SYN_REPORT, 0);
                    devOut.write(m.toByteArray()); devOut.flush();
                }
            } else {
                long dd = SystemClock.uptimeMillis();
                float x0 = (float) (cx + r1 * Math.cos(rot + Math.PI)), y0 = (float) (cy + r1 * Math.sin(rot + Math.PI));
                float x1 = (float) (cx + r1 * Math.cos(rot)), y1 = (float) (cy + r1 * Math.sin(rot));
                inject(motion(dd, dd, MotionEvent.ACTION_DOWN, new int[]{0}, new float[]{x0}, new float[]{y0}, new float[]{1f}));
                long t0 = SystemClock.uptimeMillis();
                inject(motion(dd, t0, MotionEvent.ACTION_POINTER_DOWN | (1 << MotionEvent.ACTION_POINTER_INDEX_SHIFT),
                        new int[]{0, 1}, new float[]{x0, x1}, new float[]{y0, y1}, new float[]{1f, 1f}));
                for (int s = 1; s <= steps; s++) {
                    sleepUntil(down + (durMs * s / steps));
                    float r = r1 + (r2 - r1) * s / (float) steps;
                    long t = SystemClock.uptimeMillis();
                    injectMode(motion(dd, t, MotionEvent.ACTION_MOVE, new int[]{0, 1},
                            new float[]{(float) (cx + r * Math.cos(rot + Math.PI)), (float) (cx + r * Math.cos(rot))},
                            new float[]{(float) (cy + r * Math.sin(rot + Math.PI)), (float) (cy + r * Math.sin(rot))},
                            new float[]{1f, 1f}), MODE_ASYNC);
                }
                long tu = SystemClock.uptimeMillis();
                inject(motion(dd, tu, MotionEvent.ACTION_POINTER_UP | (1 << MotionEvent.ACTION_POINTER_INDEX_SHIFT),
                        new int[]{0, 1},
                        new float[]{(float) (cx + r2 * Math.cos(rot + Math.PI)), (float) (cx + r2 * Math.cos(rot))},
                        new float[]{(float) (cy + r2 * Math.sin(rot + Math.PI)), (float) (cy + r2 * Math.sin(rot))},
                        new float[]{1f, 0f}));
                long tu2 = SystemClock.uptimeMillis();
                inject(motion(dd, tu2, MotionEvent.ACTION_UP, new int[]{0},
                        new float[]{(float) (cx + r2 * Math.cos(rot + Math.PI))},
                        new float[]{(float) (cy + r2 * Math.sin(rot + Math.PI))}, new float[]{0f}));
            }
            return "{\"ok\":true,\"events\":" + (steps + 4) + "}";
        } catch (Throwable t) {
            return fail(t);
        } finally {
            try { safeRelease(); } catch (Throwable ignored) { }
            LOCK.unlock();
        }
    }

    static String key(int code) {
        if (!LOCK.tryLock()) return busy();
        try {
            if (injMethod == null) return err("no_inject_for_keys");
            long t = SystemClock.uptimeMillis();
            inject(new KeyEvent(t, t, KeyEvent.ACTION_DOWN, code, 0, 0,
                    0, 0, 0, InputDevice.SOURCE_KEYBOARD));
            long t2 = SystemClock.uptimeMillis();
            inject(new KeyEvent(t, t2, KeyEvent.ACTION_UP, code, 0, 0,
                    0, 0, 0, InputDevice.SOURCE_KEYBOARD));
            return "{\"ok\":true}";
        } catch (Throwable t) {
            return fail(t);
        } finally { LOCK.unlock(); }
    }

    /**
     * kcm   — KeyCharacterMap.VIRTUAL_KEYBOARD.getEvents(): true key events, ASCII only.
     * multi — ACTION_MULTIPLE KeyEvent carrying the characters.
     * paste — clipboard + KEYCODE_PASTE: the only route that reliably carries CJK.
     */
    static String text(String s, String force) {
        if (!LOCK.tryLock()) return busy();
        try {
            if (injMethod == null) return err("no_inject_for_keys");
            if (s.isEmpty()) return "{\"ok\":true,\"via\":\"noop\"}";

            if (force == null || "kcm".equals(force)) {
                KeyCharacterMap map = KeyCharacterMap.load(KeyCharacterMap.VIRTUAL_KEYBOARD);
                KeyEvent[] events = map.getEvents(s.toCharArray());
                if (events != null && events.length > 0) {
                    long last = 0;
                    for (KeyEvent e : events) {
                        if (last != 0) sleepUntil(last + 4);
                        last = SystemClock.uptimeMillis();
                        inject(new KeyEvent(last, last, e.getAction(), e.getKeyCode(), e.getRepeatCount(),
                                e.getMetaState(), KeyCharacterMap.VIRTUAL_KEYBOARD, e.getScanCode(),
                                e.getFlags(), InputDevice.SOURCE_KEYBOARD));
                    }
                    return "{\"ok\":true,\"via\":\"kcm\",\"events\":" + events.length + "}";
                }
                if ("kcm".equals(force)) return err("kcm_cannot_encode");
            }
            if (force == null || "multi".equals(force)) {
                long t = SystemClock.uptimeMillis();
                inject(new KeyEvent(t, s, KeyCharacterMap.VIRTUAL_KEYBOARD, 0));
                return "{\"ok\":true,\"via\":\"multi\"}";
            }
            if ("paste".equals(force)) return paste(s);
            return err("unknown_text_route:" + force);
        } catch (Throwable t) {
            return fail(t);
        } finally { LOCK.unlock(); }
    }

    static String paste(String s) throws Exception {
        Class<?> sm = Class.forName("android.os.ServiceManager");
        Method getService = sm.getDeclaredMethod("getService", String.class);
        getService.setAccessible(true);
        IBinder binder = (IBinder) getService.invoke(null, "clipboard");
        if (binder == null) return err("no_clipboard_service");

        Class<?> stub = Class.forName("android.content.IClipboard$Stub");
        Method asInterface = stub.getDeclaredMethod("asInterface", IBinder.class);
        asInterface.setAccessible(true);
        Object cb = asInterface.invoke(null, binder);

        Class<?> clipData = Class.forName("android.content.ClipData");
        Object clip = clipData.getMethod("newPlainText", CharSequence.class, CharSequence.class)
                .invoke(null, "pfdaemon", s);

        Method setter = null;
        for (Method m : cb.getClass().getMethods())
            if ("setPrimaryClip".equals(m.getName())) { setter = m; break; }
        if (setter == null) return err("no_setPrimaryClip");

        Class<?>[] pt = setter.getParameterTypes();
        Object[] argv = new Object[pt.length];
        for (int i = 0; i < pt.length; i++) {
            if (i == 0) argv[i] = clip;
            else if (pt[i] == String.class) argv[i] = "com.android.shell";
            else if (pt[i] == int.class) argv[i] = 0;
        }
        setter.invoke(cb, argv);
        Thread.sleep(30);
        long t = SystemClock.uptimeMillis();
        inject(new KeyEvent(t, t, KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_PASTE, 0, 0,
                0, 0, 0, InputDevice.SOURCE_KEYBOARD));
        long t2 = SystemClock.uptimeMillis();
        inject(new KeyEvent(t, t2, KeyEvent.ACTION_UP, KeyEvent.KEYCODE_PASTE, 0, 0,
                0, 0, 0, InputDevice.SOURCE_KEYBOARD));
        return "{\"ok\":true,\"via\":\"paste\",\"chars\":" + s.length() + "}";
    }

    // ================================================================== probing

    static void openDevnode(String forced) {
        if (forced != null) { devPath = forced; }
        else {
            try {
                Process p = new ProcessBuilder("/system/bin/sh", "-c",
                        "getevent -pl 2>/dev/null").start();
                BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream()));
                String dev = null, name = "?";
                boolean inTouch = false;
                String line;
                while ((line = r.readLine()) != null) {
                    if (line.startsWith("add device")) {
                        dev = line.substring(line.indexOf(':') + 1).trim();
                        inTouch = false;
                    } else if (line.trim().startsWith("name:")) {
                        name = line.substring(line.indexOf(':') + 1).trim().replace("\"", "");
                    } else if (line.contains("ABS_MT_POSITION_X")) {
                        inTouch = true;
                        int mx = lastInt(line);
                        if (mx > 0) rawMaxX = mx;
                    } else if (line.contains("ABS_MT_POSITION_Y") && inTouch) {
                        int my = lastInt(line);
                        if (my > 0) rawMaxY = my;
                        if (dev != null) { devPath = dev; devName = name; }
                    }
                }
                p.waitFor();
            } catch (Throwable t) { log("getevent probe: " + t); }
        }
        if (devPath == null) devPath = "/dev/input/event7";
        try {
            devOut = new FileOutputStream(devPath);
            log("devnode opened: " + devPath);
        } catch (Throwable t) {
            devOut = null;
            log("devnode unavailable (" + devPath + "): " + t);
        }
    }

    static int lastInt(String s) {
        // "ABS_MT_POSITION_X : value 0, min 0, max 14399, fuzz 0, ..."
        int i = s.lastIndexOf("max ");
        if (i < 0) return -1;
        String sub = s.substring(i + 4).trim();
        StringBuilder sb = new StringBuilder();
        for (int k = 0; k < sub.length(); k++) {
            char c = sub.charAt(k);
            if (c >= '0' && c <= '9') sb.append(c);
            else break;
        }
        try { return Integer.parseInt(sb.toString()); } catch (Throwable t) { return -1; }
    }

    static void probeGeometry() {
        try {
            Process p = new ProcessBuilder("/system/bin/sh", "-c", "wm size").start();
            BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream()));
            String l;
            int w = -1, h = -1;
            while ((l = r.readLine()) != null) {
                if (l.contains("x") && l.contains(":")) {
                    String[] xy = l.substring(l.indexOf(':') + 1).trim().split("x");
                    try {
                        w = Integer.parseInt(xy[0].trim());
                        h = Integer.parseInt(xy[1].trim());
                    } catch (Throwable ignored) { }
                }
            }
            p.waitFor();
            if (w > 0 && h > 0) { dispW = w; dispH = h; }
        } catch (Throwable t) { log("wm size: " + t); }
    }

    static void readRotation() {
        String[] probes = {
                "dumpsys input | grep -m1 SurfaceOrientation",
                "dumpsys window | grep -m1 -o 'mRotation=[0-3]'",
                "cmd window get-rotation 2>/dev/null"
        };
        for (String cmd : probes) {
            try {
                Process p = new ProcessBuilder("/system/bin/sh", "-c", cmd).start();
                BufferedReader r = new BufferedReader(new InputStreamReader(p.getInputStream()));
                String l = r.readLine();
                p.waitFor();
                if (l == null) continue;
                String d = l.replaceAll("[^0-9]", "");
                if (d.length() == 1) {
                    int v = Integer.parseInt(d);
                    if (v >= 0 && v <= 3) { rotation = v; return; }
                }
            } catch (Throwable ignored) { }
        }
        rotation = 0;
    }

    static void bindInject() throws Exception {
        try {
            Class<?> c = Class.forName("android.hardware.input.InputManagerGlobal");
            Method g = c.getDeclaredMethod("getInstance");
            g.setAccessible(true);
            Object inst = g.invoke(null);
            Method m = c.getDeclaredMethod("injectInputEvent", android.view.InputEvent.class, int.class);
            m.setAccessible(true);
            injTarget = inst; injMethod = m;
            log("inject route: InputManagerGlobal");
            resolveDisplayId();
            return;
        } catch (Throwable t) { log("InputManagerGlobal: " + t); }

        try {
            Class<?> sm = Class.forName("android.os.ServiceManager");
            Method gs = sm.getDeclaredMethod("getService", String.class);
            gs.setAccessible(true);
            IBinder b = (IBinder) gs.invoke(null, "input");
            if (b == null) throw new IllegalStateException("no input service");
            Class<?> stub = Class.forName("android.hardware.input.IInputManager$Stub");
            Method ai = stub.getDeclaredMethod("asInterface", IBinder.class);
            ai.setAccessible(true);
            Object proxy = ai.invoke(null, b);
            Method found = null;
            for (Method m : proxy.getClass().getMethods())
                if ("injectInputEvent".equals(m.getName()) && m.getParameterTypes().length == 2) { found = m; break; }
            if (found == null) throw new IllegalStateException("no injectInputEvent");
            found.setAccessible(true);
            injTarget = proxy; injMethod = found;
            log("inject route: IInputManager(binder)");
            resolveDisplayId();
        } catch (Throwable t) { log("IInputManager: " + t); }
    }

    static void resolveDisplayId() {
        try {
            setDisplayId = MotionEvent.class.getDeclaredMethod("setDisplayId", int.class);
            setDisplayId.setAccessible(true);
        } catch (Throwable t) { setDisplayId = null; }
    }

    static void exemptHiddenApis() {
        try {
            Class<?> vmr = Class.forName("dalvik.system.VMRuntime");
            Method gr = vmr.getDeclaredMethod("getRuntime");
            gr.setAccessible(true);
            Object rt = gr.invoke(null);
            Method se = vmr.getDeclaredMethod("setHiddenApiExemptions", String[].class);
            se.setAccessible(true);
            se.invoke(rt, (Object) new String[]{"L"});
        } catch (Throwable t) { log("hidden-api exemption: " + t); }
    }

    // ===================================================================== shot

    static void writeShot(OutputStream raw) throws Exception {
        Process proc = new ProcessBuilder("/system/bin/screencap").start();
        ByteArrayOutputStream bos = new ByteArrayOutputStream(1080 * 2400 * 4 + 64);
        InputStream is = proc.getInputStream();
        byte[] buf = new byte[65536];
        int n;
        while ((n = is.read(buf)) > 0) bos.write(buf, 0, n);
        proc.waitFor();
        byte[] d = bos.toByteArray();
        int[] hdr = detectHeader(d);
        if (hdr == null) {
            raw.write(("{\"ok\":false,\"err\":\"shot_parse\",\"len\":" + d.length + "}\n").getBytes(StandardCharsets.UTF_8));
            raw.flush(); return;
        }
        int off = hdr[0], w = hdr[1], h = hdr[2], len = d.length - off;
        raw.write(("{\"ok\":true,\"w\":" + w + ",\"h\":" + h + ",\"fmt\":\"rgba\",\"len\":" + len + "}\n")
                .getBytes(StandardCharsets.UTF_8));
        raw.write(d, off, len);
        raw.flush();
    }

    static int[] detectHeader(byte[] d) {
        if (d.length < 16) return null;
        int w = le32(d, 0), h = le32(d, 4);
        if (w > 0 && h > 0 && d.length == 16 + w * h * 4) return new int[]{16, w, h};
        if (w > 0 && h > 0 && d.length == 12 + w * h * 4) return new int[]{12, w, h};
        if (w > 0 && h > 0 && w <= 20000 && h <= 20000) {
            long off = d.length - (long) w * h * 4;
            if (off >= 0 && off <= 128) return new int[]{(int) off, w, h};
        }
        return null;
    }

    static int le32(byte[] b, int o) {
        return (b[o] & 0xff) | ((b[o + 1] & 0xff) << 8) | ((b[o + 2] & 0xff) << 16) | ((b[o + 3] & 0xff) << 24);
    }

    // ==================================================================== utils

    static String busy() { return "{\"ok\":false,\"err\":\"busy\",\"detail\":\"another gesture is in flight\"}"; }
    static String err(String why) { return "{\"ok\":false,\"err\":\"" + esc(why) + "\"}"; }
    static String fail(Throwable t) {
        return "{\"ok\":false,\"err\":\"inject_failed\",\"detail\":\"" + esc(String.valueOf(t)) + "\"}";
    }

    static String esc(String s) {
        StringBuilder sb = new StringBuilder(s.length() + 8);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
            }
        }
        return sb.toString();
    }

    static int uid() { try { return android.os.Process.myUid(); } catch (Throwable t) { return -1; } }
    static int sdk() { try { return android.os.Build.VERSION.SDK_INT; } catch (Throwable t) { return -1; } }

    static void log(String s) { System.err.println("pfdaemon: " + s); System.err.flush(); }

    static void sleepUntil(long target) {
        while (true) {
            long now = SystemClock.uptimeMillis();
            long remain = target - now;
            if (remain <= 0) return;
            try {
                if (remain > 2) Thread.sleep(remain - 1);
                else Thread.yield();
            } catch (InterruptedException e) { Thread.currentThread().interrupt(); return; }
        }
    }
}
