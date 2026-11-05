// language: Java, file: OsBridge.java, target: Android 16 (SDK 36) arm64, runs as app_process under shell UID
// package dir: com/pf/os  ->  compile with: javac -classpath <android.jar> -d build/classes OsBridge.java
package com.pf.os;

// NOTE: no `import android.app.ActivityThread` — it is @hide and absent from android.jar.
// Every hidden symbol below (ActivityThread, ActivityTaskManager, IClipboard, ...) is reached by
// reflection so this compiles against the public SDK stub only.
import android.content.ClipData;
import android.content.ComponentName;
import android.content.ContentResolver;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageInfo;
import android.content.pm.ResolveInfo;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.IBinder;
import android.os.Process;
import android.provider.Settings;

import java.io.BufferedReader;
import java.io.FileDescriptor;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.lang.reflect.Field;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.locks.ReentrantLock;

/**
 * One long-lived app_process daemon. Binds the system binder services ONCE at startup and answers
 * one-line commands with one-line JSON. No JVM spawn per query, no `am`/`pm`/`settings` shell-out.
 *
 * Every service is bound through a route list: each candidate is tried in order, the first one that
 * yields a usable interface wins, and the winning route name is recorded so `diag` can report it.
 */
public final class OsBridge {

    // ---------------------------------------------------------------- state

    static Object sAtm;                 // IActivityTaskManager
    static Object sAm;                  // IActivityManager
    static Object sPm;                  // IPackageManager
    static Object sWm;                  // IWindowManager
    static Object sCb;                  // IClipboard
    static Context sCtx;                // system context from ActivityThread.systemMain()

    static final LinkedHashMap<String, String> ROUTES = new LinkedHashMap<>();
    static final List<String> NOTES = new ArrayList<>();

    static final ReentrantLock EXEC = new ReentrantLock(true);
    static final AtomicInteger CONNS = new AtomicInteger();
    static final int MAX_CONNS = 4;     // bounded: no unbounded queue, no thread pile-up

    static boolean sQuit = false;
    static int sPort = 0;

    // ---------------------------------------------------------------- main

    public static void main(String[] argv) {
        try {
            parseArgs(argv);
            boot();
            if (sQuit) return;
            serve(sPort);
        } catch (Throwable t) {
            System.err.println("fatal: " + t);
            t.printStackTrace();
            System.exit(2);
        }
    }

    static void parseArgs(String[] argv) {
        for (int i = 0; i < argv.length; i++) {
            String a = argv[i];
            if ("--port".equals(a) && i + 1 < argv.length) {
                sPort = Integer.parseInt(argv[++i].trim());
            } else if ("--diag-once".equals(a)) {
                sQuit = true;                       // boot, print diag to stdout, exit
            }
        }
        if (sPort == 0 && !sQuit) sPort = 27183;
    }

    // ---------------------------------------------------------------- boot: bind everything once

    static void boot() {
        long t0 = System.nanoTime();
        NOTES.add("process_uid=" + Process.myUid());
        NOTES.add("sdk=" + Build.VERSION.SDK_INT + " release=" + Build.VERSION.RELEASE);

        // system context first: settings + package manager fallbacks hang off it
        try {
            sCtx = bindSystemContext();
        } catch (Throwable t) {
            ROUTES.put("system_context", "FAIL " + brief(t));
        }

        bind("IActivityTaskManager", new Route[]{
                new Route("ActivityTaskManager.getService()") {
                    Object run() throws Exception { return staticNoArg("android.app.ActivityTaskManager", "getService"); }
                },
                new Route("IActivityTaskManager$Stub.asInterface(SM.activity_task)") {
                    Object run() throws Exception { return stubAsInterface("android.app.IActivityTaskManager$Stub", svc("activity_task")); }
                },
                new Route("IActivityTaskManager$Stub.asInterface(SM.activity)") {
                    Object run() throws Exception { return stubAsInterface("android.app.IActivityTaskManager$Stub", svc("activity")); }
                },
        }, v -> sAtm = v);

        bind("IActivityManager", new Route[]{
                new Route("ActivityManager.getService()") {
                    Object run() throws Exception { return staticNoArg("android.app.ActivityManager", "getService"); }
                },
                new Route("IActivityManager$Stub.asInterface(SM.activity)") {
                    Object run() throws Exception { return stubAsInterface("android.app.IActivityManager$Stub", svc("activity")); }
                },
        }, v -> sAm = v);

        bind("IPackageManager", new Route[]{
                new Route("AppGlobals.getPackageManager()") {
                    Object run() throws Exception { return staticNoArg("android.app.AppGlobals", "getPackageManager"); }
                },
                new Route("IPackageManager$Stub.asInterface(SM.package)") {
                    Object run() throws Exception { return stubAsInterface("android.content.pm.IPackageManager$Stub", svc("package")); }
                },
        }, v -> sPm = v);

        bind("IWindowManager", new Route[]{
                new Route("WindowManagerGlobal.getWindowManagerService()") {
                    Object run() throws Exception { return staticNoArg("android.view.WindowManagerGlobal", "getWindowManagerService"); }
                },
                new Route("IWindowManager$Stub.asInterface(SM.window)") {
                    Object run() throws Exception { return stubAsInterface("android.view.IWindowManager$Stub", svc("window")); }
                },
        }, v -> sWm = v);

        bind("IClipboard", new Route[]{
                new Route("IClipboard$Stub.asInterface(SM.clipboard)") {
                    Object run() throws Exception { return stubAsInterface("android.content.IClipboard$Stub", svc("clipboard")); }
                },
        }, v -> sCb = v);

        long ms = (System.nanoTime() - t0) / 1_000_000L;
        bootMs = ms;
        NOTES.add("boot_ms=" + ms);
        if (sQuit) {
            System.out.println(diagJson());
        } else {
            System.out.println("osbridge up: uid=" + Process.myUid() + " sdk=" + Build.VERSION.SDK_INT
                    + " port=" + sPort + " boot_ms=" + ms);
            for (Map.Entry<String, String> e : ROUTES.entrySet()) {
                System.out.println("  route " + e.getKey() + " <- " + e.getValue());
            }
        }
    }

    interface Sink { void accept(Object v); }

    abstract static class Route {
        final String name;
        Route(String n) { name = n; }
        abstract Object run() throws Exception;
        @Override public String toString() { return name; }
    }

    static void bind(String svcName, Route[] routes, Sink sink) {
        List<String> tried = new ArrayList<>();
        for (Route r : routes) {
            try {
                Object v = r.run();
                if (v != null) {
                    sink.accept(v);
                    ROUTES.put(svcName, r.name);
                    return;
                }
                tried.add(r.name + "=null");
            } catch (Throwable t) {
                tried.add(r.name + "=" + brief(t));
            }
        }
        ROUTES.put(svcName, "UNREACHABLE " + String.join(" | ", tried));
    }

    /**
     * ActivityThread.systemMain() gives a real Context (package "android") backed by a live
     * IActivityManager. Its constructor builds Handlers, so the calling thread must own a Looper:
     * prepareMainLooper() here, then run the queue on a daemon thread so nothing ever blocks on it.
     * (Verified on SDK 36: without this, systemMain throws "Can't create handler inside thread
     * Thread[main,5,main] that has not called Looper.prepare()".)
     */
    static Context bindSystemContext() throws Exception {
        android.os.Looper.prepareMainLooper();
        Thread looper = new Thread(() -> {
            try { android.os.Looper.loop(); } catch (Throwable ignored) { }
        }, "sys-looper");
        looper.setDaemon(true);
        looper.start();

        Class<?> at = Class.forName("android.app.ActivityThread");
        Method m = at.getDeclaredMethod("systemMain");
        m.setAccessible(true);
        Object thread = m.invoke(null);
        Method getCtx = at.getDeclaredMethod("getSystemContext");
        getCtx.setAccessible(true);
        Context c = (Context) getCtx.invoke(thread);
        if (c == null) throw new IllegalStateException("getSystemContext() returned null");
        ROUTES.put("system_context", "ActivityThread.systemMain().getSystemContext()");
        return c;
    }

    // ---------------------------------------------------------------- reflection plumbing

    static String brief(Throwable t) {
        if (t instanceof InvocationTargetException && t.getCause() != null) t = t.getCause();
        String n = t.getClass().getSimpleName();
        String msg = t.getMessage();
        if (msg == null) msg = "";
        msg = msg.replace('\n', ' ').replace('\r', ' ');
        if (msg.length() > 120) msg = msg.substring(0, 120) + "...";
        return n + (msg.isEmpty() ? "" : ": " + msg);
    }

    static Object staticNoArg(String cls, String method) throws Exception {
        Class<?> c = Class.forName(cls);
        Method m = c.getDeclaredMethod(method);
        m.setAccessible(true);
        return m.invoke(null);
    }

    static IBinder svc(String name) throws Exception {
        Class<?> sm = Class.forName("android.os.ServiceManager");
        Method m = sm.getDeclaredMethod("getService", String.class);
        m.setAccessible(true);
        IBinder b = (IBinder) m.invoke(null, name);
        if (b == null) throw new IllegalStateException("ServiceManager returned null for \"" + name + "\"");
        return b;
    }

    static Object stubAsInterface(String stubCls, IBinder b) throws Exception {
        Class<?> c = Class.forName(stubCls);
        for (Method m : c.getDeclaredMethods()) {
            if (!"asInterface".equals(m.getName())) continue;
            Class<?>[] p = m.getParameterTypes();
            if (p.length == 1 && p[0].isInstance(b)) {
                m.setAccessible(true);
                return m.invoke(null, b);
            }
        }
        throw new NoSuchMethodException(stubCls + "#asInterface(IBinder)");
    }

    static Class<?> wrap(Class<?> p) {
        if (!p.isPrimitive()) return p;
        if (p == int.class) return Integer.class;
        if (p == long.class) return Long.class;
        if (p == boolean.class) return Boolean.class;
        if (p == float.class) return Float.class;
        if (p == double.class) return Double.class;
        if (p == short.class) return Short.class;
        if (p == byte.class) return Byte.class;
        if (p == char.class) return Character.class;
        return Void.class;
    }

    /** Find a method by name whose parameter list accepts these argument objects (null matches any reference type). */
    static Method findMethod(Class<?> c, String name, Object[] args) {
        outer:
        for (Method m : c.getMethods()) {
            if (!m.getName().equals(name)) continue;
            Class<?>[] p = m.getParameterTypes();
            if (p.length != args.length) continue;
            for (int i = 0; i < p.length; i++) {
                if (args[i] == null) { if (p[i].isPrimitive()) continue outer; }
                else if (!wrap(p[i]).isInstance(args[i])) continue outer;
            }
            m.setAccessible(true);
            return m;
        }
        return null;
    }

    static final class Res {
        final Object value;
        final Method method;
        Res(Object v, Method m) { value = v; method = m; }
    }

    /** Try each candidate argument list in order; first one that exists AND invokes cleanly wins. */
    static Res invokeAny(Object target, String name, List<Object[]> candidates) throws Exception {
        List<String> errs = new ArrayList<>();
        for (Object[] args : candidates) {
            Method m = findMethod(target.getClass(), name, args);
            if (m == null) { errs.add("no-sig/" + args.length); continue; }
            try {
                Object v = m.invoke(target, args);
                return new Res(v, m);
            } catch (InvocationTargetException ite) {
                throw rethrow(ite);
            } catch (Throwable t) {
                errs.add(brief(t));
            }
        }
        throw new NoSuchMethodException(name + " matched no route [tried: " + String.join(",", errs) + "]");
    }

    static Exception rethrow(InvocationTargetException ite) {
        Throwable c = ite.getCause();
        if (c instanceof RuntimeException) throw (RuntimeException) c;
        if (c instanceof Error) throw (Error) c;
        return new Exception(c == null ? ite.toString() : c.getClass().getSimpleName() + ": " + c.getMessage(), c);
    }

    static Object field(Object o, String name) throws Exception {
        Class<?> c = o.getClass();
        while (c != null) {
            try {
                Field f = c.getDeclaredField(name);
                f.setAccessible(true);
                return f.get(o);
            } catch (NoSuchFieldException nsf) {
                c = c.getSuperclass();
            }
        }
        throw new NoSuchFieldException(o.getClass().getName() + "." + name);
    }

    static String sig(Method m) {
        StringBuilder sb = new StringBuilder(m.getName()).append('(');
        Class<?>[] p = m.getParameterTypes();
        for (int i = 0; i < p.length; i++) {
            if (i > 0) sb.append(',');
            sb.append(p[i].getSimpleName());
        }
        return sb.append(')').toString();
    }

    static String listSigs(String cls, String contains) {
        StringBuilder sb = new StringBuilder();
        try {
            Class<?> c = Class.forName(cls);
            String needle = contains.toLowerCase(Locale.ROOT);
            for (Method m : c.getDeclaredMethods()) {
                if (!m.getName().toLowerCase(Locale.ROOT).contains(needle)) continue;
                if (sb.length() > 0) sb.append(';');
                sb.append(sig(m));
            }
        } catch (Throwable t) {
            sb.append("!").append(brief(t));
        }
        return sb.toString();
    }

    // ---------------------------------------------------------------- JSON out

    static final class Out {
        final StringBuilder sb = new StringBuilder(192);
        Out(boolean ok) { sb.append("{\"ok\":").append(ok); }
        Out put(String k, Object v) {
            sb.append(',').append(quote(k)).append(':').append(val(v));
            return this;
        }
        Out raw(String k, String json) {
            sb.append(',').append(quote(k)).append(':').append(json);
            return this;
        }
        String done() { return sb.append('}').toString(); }

        static String val(Object v) {
            if (v == null) return "null";
            if (v instanceof Number || v instanceof Boolean) return v.toString();
            return quote(String.valueOf(v));
        }

        static String quote(String s) {
            StringBuilder b = new StringBuilder(s.length() + 16);
            b.append('"');
            for (int i = 0; i < s.length(); i++) {
                char ch = s.charAt(i);
                switch (ch) {
                    case '"': b.append("\\\""); break;
                    case '\\': b.append("\\\\"); break;
                    case '\n': b.append("\\n"); break;
                    case '\r': b.append("\\r"); break;
                    case '\t': b.append("\\t"); break;
                    default:
                        if (ch < 0x20) b.append(String.format(Locale.ROOT, "\\u%04x", (int) ch));
                        else b.append(ch);
                }
            }
            return b.append('"').toString();
        }
    }

    static String err(Throwable t) {
        return new Out(false).put("error", brief(t)).done();
    }

    static String okJson() { return new Out(true).done(); }

    // ---------------------------------------------------------------- command dispatch

    static String exec(String line) {
        String s = line == null ? "" : line.trim();
        if (s.isEmpty()) return err(new IllegalArgumentException("empty command"));
        List<String> tok = tokenize(s);
        if (tok.isEmpty()) return err(new IllegalArgumentException("empty command"));
        String cmd = tok.get(0);
        String rest = s.substring(cmd.length()).trim();

        try {
            switch (cmd) {
                case "ping":            return cmdPing();
                case "focus":           return cmdFocus();
                case "launch":          return cmdLaunch(tok, rest);
                case "force_stop":      return cmdForceStop(tok);
                case "packages":        return cmdPackages(tok);
                case "pkg_info":        return cmdPkgInfo(tok);
                case "resolve":         return cmdResolve(rest);
                case "setting_get":     return cmdSettingGet(tok);
                case "setting_put":     return cmdSettingPut(tok);
                case "clipboard_get":   return cmdClipboardGet();
                case "clipboard_set":   return cmdClipboardSet(tok);
                case "broadcast":       return cmdBroadcast(rest);
                case "services":        return cmdServices(tok);
                case "activity_top":    return cmdActivityTop();
                case "diag":            return diagJson();
                case "quit":            return cmdQuit();
                case "uname":           return cmdUname();
                default:
                    return err(new IllegalArgumentException("unknown command: " + cmd));
            }
        } catch (Throwable t) {
            return err(t);
        }
    }

    /** Split on whitespace, honouring "double quoted" arguments. */
    static List<String> tokenize(String s) {
        List<String> out = new ArrayList<>();
        StringBuilder cur = new StringBuilder();
        boolean inQ = false, any = false;
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c == '"') { inQ = !inQ; any = true; continue; }
            if (!inQ && Character.isWhitespace(c)) {
                if (any || cur.length() > 0) { out.add(cur.toString()); cur.setLength(0); any = false; }
                continue;
            }
            cur.append(c);
            any = true;
        }
        if (any || cur.length() > 0) out.add(cur.toString());
        return out;
    }

    static void need(boolean cond, String msg) {
        if (!cond) throw new IllegalArgumentException(msg);
    }

    static int argInt(List<String> tok, int idx, int dflt) {
        if (tok.size() <= idx) return dflt;
        try { return Integer.parseInt(tok.get(idx)); } catch (NumberFormatException e) { return dflt; }
    }

    // ---------------------------------------------------------------- commands

    static String cmdPing() {
        return new Out(true)
                .put("uid", Process.myUid())
                .put("sdk", Build.VERSION.SDK_INT)
                .put("pid", Process.myPid())
                .put("boot_ms", bootMs)
                .done();
    }

    static long bootMs = -1;

    static String cmdUname() {
        return new Out(true)
                .put("release", Build.VERSION.RELEASE)
                .put("sdk", Build.VERSION.SDK_INT)
                .put("build", Build.ID)
                .put("device", Build.MODEL)
                .done();
    }

    static String cmdQuit() {
        System.out.println("quit requested");
        Thread t = new Thread(() -> {
            try { Thread.sleep(120); } catch (InterruptedException ignored) { }
            System.out.flush();
            Runtime.getRuntime().halt(0);
        });
        t.setDaemon(true);
        t.start();
        return okJson();
    }

    /**
     * Focused package/activity come from IActivityTaskManager.getTasks(1): the top of the top task is
     * the foreground activity. `window` is derived from that component (documented: it is not read
     * back out of WindowManagerService, which exposes no AIDL getter for mCurrentFocus).
     */
    static ComponentName topComponent() throws Exception {
        need(sAtm != null, "IActivityTaskManager not bound");
        List<Object[]> cands = new ArrayList<>();
        // SDK 36 verified: getTasks(int maxNum, boolean filterOnlyVisibleRecents, boolean keepIntentExtra, int displayId)
        cands.add(new Object[]{1, false, false, 0});
        cands.add(new Object[]{1, false, false});
        cands.add(new Object[]{1, false});
        cands.add(new Object[]{1});
        Res r = invokeAny(sAtm, "getTasks", cands);
        Object v = r.value;
        List<?> tasks;
        if (v instanceof List) tasks = (List<?>) v;
        else {
            Object l = invokeAny(v, "getList", single(null)).value;
            tasks = (List<?>) l;
        }
        if (tasks == null || tasks.isEmpty()) return null;
        Object info = tasks.get(0);
        Object cn = field(info, "topActivity");
        if (cn == null) cn = field(info, "baseActivity");
        return (ComponentName) cn;
    }

    static String cmdFocus() {
        try {
            ComponentName cn = topComponent();
            if (cn == null) return new Out(true).put("package", null).put("activity", null).put("window", null).done();
            return new Out(true)
                    .put("package", cn.getPackageName())
                    .put("activity", cn.getClassName())
                    .put("window", cn.flattenToShortString())
                    .done();
        } catch (Throwable t) {
            return err(t);
        }
    }

    static String cmdLaunch(List<String> tok, String rest) throws Exception {
        need(tok.size() >= 2, "usage: launch <pkg> [/<activity>] [--wait]");
        String pkg = tok.get(1);
        String act = null;
        boolean wait = false;
        for (int i = 2; i < tok.size(); i++) {
            String a = tok.get(i);
            if ("--wait".equals(a)) { wait = true; continue; }
            if (a.startsWith("/") || a.startsWith(".") || a.contains(".")) { act = a; continue; }
        }
        need(pkg.indexOf(' ') < 0, "bad package");

        String comp = resolveLaunchComponent(pkg, act);
        Intent intent = new Intent(Intent.ACTION_MAIN);
        if (comp != null) {
            int slash = comp.indexOf('/');
            String p = comp.substring(0, slash);
            String c = comp.substring(slash + 1);
            if (c.startsWith(".")) c = p + c;
            intent.setComponent(new ComponentName(p, c));
        } else {
            intent.setPackage(pkg);
            intent.addCategory(Intent.CATEGORY_LAUNCHER);
        }
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);

        long start = System.nanoTime();
        startActivityNow(intent, pkg, 0);
        long dispatched = (System.nanoTime() - start) / 1_000_000L;

        Long achieved = null;
        if (wait) {
            long deadline = System.nanoTime() + 3_000_000_000L;
            while (System.nanoTime() < deadline) {
                try {
                    ComponentName cn = topComponent();
                    if (cn != null && pkg.equals(cn.getPackageName())) {
                        achieved = (System.nanoTime() - start) / 1_000_000L;
                        break;
                    }
                } catch (Throwable ignored) { }
                Thread.sleep(25);
            }
        }
        Out o = new Out(true).put("ms", wait ? (achieved == null ? -1L : achieved) : dispatched)
                .put("dispatch_ms", dispatched)
                .put("package", pkg)
                .put("component", comp);
        if (wait) o.put("focused", achieved != null);
        return o.done();
    }

    /** package-only launch: resolve ACTION_MAIN/CATEGORY_LAUNCHER through IPackageManager. */
    static String resolveLaunchComponent(String pkg, String act) {
        if (act != null) return pkg + "/" + act;
        try {
            Intent q = new Intent(Intent.ACTION_MAIN);
            q.addCategory(Intent.CATEGORY_LAUNCHER);
            q.setPackage(pkg);
            List<Object[]> cands = new ArrayList<>();
            cands.add(new Object[]{q, null, 0L, 0});
            cands.add(new Object[]{q, null, 0, 0});
            cands.add(new Object[]{q, null, 0L});
            Res r = invokeAny(sPm, "queryIntentActivities", cands);
            List<?> list = asList(r.value);
            if (list.isEmpty()) return null;
            ResolveInfo ri = (ResolveInfo) list.get(0);
            if (ri.activityInfo == null) return null;
            return ri.activityInfo.packageName + "/" + ri.activityInfo.name;
        } catch (Throwable t) {
            NOTES.add("resolveLaunchComponent failed: " + brief(t));
            return null;
        }
    }

    static List<Object[]> single(Object[] a) {
        List<Object[]> l = new ArrayList<>();
        l.add(a);
        return l;
    }

    @SuppressWarnings("unchecked")
    static List<?> asList(Object v) throws Exception {
        if (v == null) return new ArrayList<>();
        if (v instanceof List) return (List<?>) v;
        Object l = invokeAny(v, "getList", single(null)).value;
        return (List<?>) l;
    }

    /**
     * Direct IActivityTaskManager.startActivity. Candidate signatures differ per API level; the first
     * one that exists and invokes cleanly wins and its signature is recorded in NOTES for `diag`.
     */
    static void startActivityNow(Intent intent, String callingPkg, int flags) throws Exception {
        need(sAtm != null, "IActivityTaskManager not bound");
        List<Object[]> cands = new ArrayList<>();
        // Android 10..16: (IApplicationThread, String pkg, String featureId, Intent, String resolvedType,
        //                  IBinder resultTo, String resultWho, int requestCode, int flags, ProfilerInfo, Bundle)
        cands.add(new Object[]{null, callingPkg, null, intent, null, null, null, 0, flags, null, null});
        // pre-featureId (Android 8/9)
        cands.add(new Object[]{null, callingPkg, intent, null, null, null, 0, flags, null, null});
        // with explicit userId appended
        cands.add(new Object[]{null, callingPkg, null, intent, null, null, null, 0, flags, null, null, 0});
        cands.add(new Object[]{0, null, callingPkg, null, intent, null, null, null, 0, flags, null, null});
        Res r = invokeAny(sAtm, "startActivity", cands);
        NOTES.add("startActivity bound: " + sig(r.method) + " -> " + r.value);
    }

    static String cmdForceStop(List<String> tok) throws Exception {
        need(tok.size() >= 2, "usage: force_stop <pkg>");
        need(sAm != null, "IActivityManager not bound");
        String pkg = tok.get(1);
        List<Object[]> cands = new ArrayList<>();
        cands.add(new Object[]{pkg, 0});
        cands.add(new Object[]{pkg});
        invokeAny(sAm, "forceStopPackage", cands);
        return okJson();
    }

    static String cmdPackages(List<String> tok) throws Exception {
        need(sPm != null, "IPackageManager not bound");
        String filter = null;
        boolean systemOnly = false;
        for (int i = 1; i < tok.size(); i++) {
            String a = tok.get(i);
            if ("--filter".equals(a) && i + 1 < tok.size()) filter = tok.get(++i);
            else if ("--system".equals(a)) systemOnly = true;
        }
        long pflags = 0L;
        List<Object[]> cands = new ArrayList<>();
        cands.add(new Object[]{pflags, 0});
        cands.add(new Object[]{(int) pflags, 0});
        cands.add(new Object[]{pflags});
        Res r;
        try {
            r = invokeAny(sPm, "getInstalledPackages", cands);
        } catch (Throwable t) {
            // shell lacks some flags; retry with MATCH_UNINSTALLED_PACKAGES off + defaults
            cands.clear();
            cands.add(new Object[]{0x00002000L, 0});   // MATCH_UNINSTALLED_PACKAGES
            cands.add(new Object[]{0x00002000, 0});
            r = invokeAny(sPm, "getInstalledPackages", cands);
        }
        List<?> list = asList(r.value);
        ArrayList<String> names = new ArrayList<>(list.size());
        String f = filter == null ? null : filter.toLowerCase(Locale.ROOT);
        for (Object o : list) {
            PackageInfo pi = (PackageInfo) o;
            if (pi == null || pi.packageName == null) continue;
            if (f != null && !pi.packageName.toLowerCase(Locale.ROOT).contains(f)) continue;
            if (systemOnly) {
                ApplicationInfo ai = pi.applicationInfo;
                if (ai == null || (ai.flags & ApplicationInfo.FLAG_SYSTEM) == 0) continue;
            }
            names.add(pi.packageName);
        }
        names.sort(String::compareTo);
        StringBuilder arr = new StringBuilder("[");
        for (int i = 0; i < names.size(); i++) {
            if (i > 0) arr.append(',');
            arr.append(Out.quote(names.get(i)));
        }
        arr.append(']');
        return new Out(true).put("count", names.size()).raw("items", arr.toString()).done();
    }

    static String cmdPkgInfo(List<String> tok) throws Exception {
        need(tok.size() >= 2, "usage: pkg_info <pkg>");
        need(sPm != null, "IPackageManager not bound");
        String pkg = tok.get(1);
        List<Object[]> cands = new ArrayList<>();
        cands.add(new Object[]{pkg, 0L, 0});
        cands.add(new Object[]{pkg, 0, 0});
        cands.add(new Object[]{pkg, 0L});
        Res r = invokeAny(sPm, "getPackageInfo", cands);
        PackageInfo pi = (PackageInfo) r.value;
        if (pi == null) return new Out(false).put("error", "package not found: " + pkg).done();
        ApplicationInfo ai = pi.applicationInfo;
        long vc;
        try { vc = pi.getLongVersionCode(); } catch (Throwable t) { vc = pi.versionCode; }
        return new Out(true)
                .put("version_name", pi.versionName)
                .put("version_code", vc)
                .put("uid", ai == null ? null : ai.uid)
                .put("target_sdk", ai == null ? null : ai.targetSdkVersion)
                .put("apk_path", ai == null ? null : ai.sourceDir)
                .put("package", pi.packageName)
                .done();
    }

    static Intent intentFromJson(String json) throws Exception {
        org.json.JSONObject o = new org.json.JSONObject(json);
        Intent it = new Intent();
        if (o.has("action")) it.setAction(o.getString("action"));
        if (o.has("data")) it.setData(Uri.parse(o.getString("data")));
        if (o.has("type")) it.setType(o.getString("type"));
        if (o.has("package")) it.setPackage(o.getString("package"));
        if (o.has("component")) {
            String c = o.getString("component");
            int slash = c.indexOf('/');
            need(slash > 0, "component must be pkg/cls");
            String p = c.substring(0, slash), cls = c.substring(slash + 1);
            if (cls.startsWith(".")) cls = p + cls;
            it.setComponent(new ComponentName(p, cls));
        }
        if (o.has("flags")) it.setFlags(o.getInt("flags"));
        if (o.has("categories")) {
            org.json.JSONArray arr = o.getJSONArray("categories");
            for (int i = 0; i < arr.length(); i++) it.addCategory(arr.getString(i));
        }
        if (o.has("extras")) {
            org.json.JSONObject ex = o.getJSONObject("extras");
            Bundle b = new Bundle();
            for (java.util.Iterator<String> k = ex.keys(); k.hasNext(); ) {
                String key = k.next();
                Object v = ex.get(key);
                if (v instanceof Integer) b.putInt(key, (Integer) v);
                else if (v instanceof Long) b.putLong(key, (Long) v);
                else if (v instanceof Boolean) b.putBoolean(key, (Boolean) v);
                else if (v instanceof Double) b.putDouble(key, (Double) v);
                else b.putString(key, String.valueOf(v));
            }
            it.putExtras(b);
        }
        return it;
    }

    static String cmdResolve(String rest) throws Exception {
        need(!rest.isEmpty(), "usage: resolve <intent-json>");
        need(sPm != null, "IPackageManager not bound");
        Intent it = intentFromJson(rest);
        List<Object[]> cands = new ArrayList<>();
        cands.add(new Object[]{it, null, 0L, 0});
        cands.add(new Object[]{it, null, 0, 0});
        cands.add(new Object[]{it, null, 0L});
        Res r = invokeAny(sPm, "resolveIntent", cands);
        ResolveInfo ri = (ResolveInfo) r.value;
        if (ri == null) return new Out(false).put("error", "no activity resolves").done();
        String comp = null, act = null;
        if (ri.activityInfo != null) {
            comp = ri.activityInfo.packageName + "/" + ri.activityInfo.name;
            act = ri.activityInfo.name;
        }
        return new Out(true).put("component", comp).put("activity", act).put("package", ri.resolvePackageName).done();
    }

    static String cmdSettingGet(List<String> tok) throws Exception {
        need(tok.size() >= 3, "usage: setting_get <system|secure|global> <key>");
        need(sCtx != null, "system context not bound");
        String ns = tok.get(1), key = tok.get(2);
        String v;
        ContentResolver cr = sCtx.getContentResolver();
        switch (ns) {
            case "system": v = Settings.System.getString(cr, key); break;
            case "secure": v = Settings.Secure.getString(cr, key); break;
            case "global": v = Settings.Global.getString(cr, key); break;
            default: throw new IllegalArgumentException("namespace must be system|secure|global");
        }
        return new Out(true).put("value", v).put("exists", v != null).done();
    }

    static String cmdSettingPut(List<String> tok) throws Exception {
        need(tok.size() >= 3, "usage: setting_put <system|secure|global> <key> <value>");
        need(sCtx != null, "system context not bound");
        String ns = tok.get(1), key = tok.get(2);
        // value = everything after the key, so values with spaces survive
        int kAt = indexOfNth(tok, key, 1);
        StringBuilder vb = new StringBuilder();
        for (int i = kAt + 1; i < tok.size(); i++) {
            if (vb.length() > 0) vb.append(' ');
            vb.append(tok.get(i));
        }
        String value = vb.toString();
        ContentResolver cr = sCtx.getContentResolver();
        boolean ok;
        switch (ns) {
            case "system": ok = Settings.System.putString(cr, key, value); break;
            case "secure": ok = Settings.Secure.putString(cr, key, value); break;
            case "global": ok = Settings.Global.putString(cr, key, value); break;
            default: throw new IllegalArgumentException("namespace must be system|secure|global");
        }
        return new Out(true).put("written", ok).put("value", value).done();
    }

    static int indexOfNth(List<String> tok, String val, int nth) {
        int seen = 0;
        for (int i = 0; i < tok.size(); i++) {
            if (tok.get(i).equals(val)) {
                if (seen == nth) return i;
                seen++;
            }
        }
        return tok.size() - 1;
    }

    static int myUserId() {
        try { return (int) staticNoArg("android.os.UserHandle", "myUserId"); } catch (Throwable t) { return 0; }
    }

    /**
     * IClipboard over binder. On Android 10+ the framework refuses clipboard reads to a process that
     * is not the focused app unless it holds READ_CLIPBOARD_IN_BACKGROUND; shell does not, so a real
     * "no focused window" denial is reported as ok:true with null text and a "denied" marker instead
     * of being papered over.
     */
    static String cmdClipboardGet() throws Exception {
        need(sCb != null, "IClipboard not bound");
        int uid = myUserId();
        List<Object[]> cands = new ArrayList<>();
        // SDK 36 verified: getPrimaryClip(String callingPackage, String attributionTag, int userId, int deviceId)
        cands.add(new Object[]{"com.android.shell", null, uid, 0});
        cands.add(new Object[]{"com.android.shell", "com.android.shell", uid, 0});
        cands.add(new Object[]{"com.android.shell", null, uid});
        cands.add(new Object[]{"com.android.shell", null});
        Object clip;
        try {
            clip = invokeAny(sCb, "getPrimaryClip", cands).value;
        } catch (Throwable t) {
            return new Out(false).put("error", brief(t)).put("note", "clipboard read denied").done();
        }
        if (clip == null) {
            return new Out(true).put("text", null).put("denied", true)
                    .put("note", "IClipboard returned null (not focused / no READ_CLIPBOARD_IN_BACKGROUND)").done();
        }
        ClipData cd = (ClipData) clip;
        StringBuilder sb = new StringBuilder();
        if (cd.getItemCount() > 0) {
            ClipData.Item it = cd.getItemAt(0);
            CharSequence cs = it.getText();
            if (cs != null) sb.append(cs);
            else if (it.getUri() != null) sb.append(it.getUri());
        }
        return new Out(true).put("text", sb.toString()).put("items", cd.getItemCount()).done();
    }

    static String cmdClipboardSet(List<String> tok) throws Exception {
        need(tok.size() >= 2, "usage: clipboard_set <base64-utf8>");
        need(sCb != null, "IClipboard not bound");
        String text = new String(Base64.getDecoder().decode(tok.get(1)), StandardCharsets.UTF_8);
        ClipData cd = ClipData.newPlainText("osbridge", text);
        int uid = myUserId();
        List<Object[]> cands = new ArrayList<>();
        cands.add(new Object[]{cd, "com.android.shell", null, uid, 0});
        cands.add(new Object[]{cd, "com.android.shell", null, uid});
        cands.add(new Object[]{cd, "com.android.shell", null});
        invokeAny(sCb, "setPrimaryClip", cands);
        return new Out(true).put("bytes", text.getBytes(StandardCharsets.UTF_8).length).done();
    }

    static String cmdBroadcast(String rest) throws Exception {
        need(!rest.isEmpty(), "usage: broadcast <intent-json>");
        Intent it = intentFromJson(rest);
        if (!it.hasCategory(Intent.CATEGORY_DEFAULT) && it.getCategories() == null) {
            // leave as-is; broadcasts without a category are fine
        }
        // Route 1: raw IActivityManager.broadcastIntent (signature varies hard across levels)
        if (sAm != null) {
            List<Object[]> cands = new ArrayList<>();
            // SDK 36 verified: broadcastIntent(IApplicationThread, Intent, String, IIntentReceiver, int,
            //   String, Bundle, String[], int appOp, Bundle, boolean serialized, boolean sticky, int userId)
            cands.add(new Object[]{null, it, null, null, 0, null, null, null, -1, null, false, false, 0});
            cands.add(new Object[]{null, it, null, null, 0, null, null, null, null, 0});
            cands.add(new Object[]{null, it, null, null, 0, null, null, null, 0});
            try {
                Res r = invokeAny(sAm, "broadcastIntent", cands);
                NOTES.add("broadcast bound: " + sig(r.method));
                return new Out(true).put("route", "binder:IActivityManager.broadcastIntent").done();
            } catch (Throwable t) {
                NOTES.add("broadcastIntent raw failed: " + brief(t));
            }
        }
        // Route 2: system context sendBroadcast (same binder call, resolved by ContextImpl)
        need(sCtx != null, "no broadcast route available");
        sCtx.sendBroadcast(it);
        return new Out(true).put("route", "systemContext.sendBroadcast").done();
    }

    /**
     * Running app services visible to shell. IActivityManager.getServices is filtered for non-system
     * callers on O+; whatever comes back is returned verbatim and `visible` says how many the platform
     * chose to hand over, so an empty list is never dressed up as "no services running".
     */
    static String cmdServices(List<String> tok) throws Exception {
        need(sAm != null, "IActivityManager not bound");
        String filter = tok.size() >= 2 ? tok.get(1).toLowerCase(Locale.ROOT) : null;
        List<Object[]> cands = new ArrayList<>();
        cands.add(new Object[]{200, 0});
        cands.add(new Object[]{200});
        Res r = invokeAny(sAm, "getServices", cands);
        List<?> list = asList(r.value);
        StringBuilder arr = new StringBuilder("[");
        int n = 0;
        for (Object o : list) {
            ComponentName cn = (ComponentName) field(o, "service");
            if (cn == null) continue;
            String name = cn.flattenToShortString();
            if (filter != null && !name.toLowerCase(Locale.ROOT).contains(filter)) continue;
            if (n++ > 0) arr.append(',');
            arr.append('{')
               .append("\"name\":").append(Out.quote(name)).append(',')
               .append("\"pid\":").append(field(o, "pid")).append(',')
               .append("\"foreground\":").append(Boolean.TRUE.equals(field(o, "foreground")))
               .append('}');
        }
        arr.append(']');
        return new Out(true).put("count", n).raw("items", arr.toString())
                .put("visible", list.size()).done();
    }

    static String cmdActivityTop() throws Exception {
        ComponentName cn = topComponent();
        if (cn == null) return new Out(false).put("error", "no top activity").done();
        int pid = pidOfPackage(cn.getPackageName());
        return new Out(true)
                .put("package", cn.getPackageName())
                .put("activity", cn.getClassName())
                .put("pid", pid)
                .done();
    }

    static int pidOfPackage(String pkg) {
        if (sAm != null) {
            try {
                List<Object[]> c = new ArrayList<>();
                c.add(new Object[0]);
                Res r = invokeAny(sAm, "getRunningAppProcesses", c);
                for (Object p : asList(r.value)) {
                    String pn = (String) field(p, "processName");
                    if (pn != null && (pn.equals(pkg) || pn.startsWith(pkg + ":"))) {
                        return (Integer) field(p, "pid");
                    }
                }
            } catch (Throwable t) {
                NOTES.add("getRunningAppProcesses failed: " + brief(t));
            }
        }
        // /proc fallback: read cmdline of each pid
        try {
            java.io.File[] pids = new java.io.File("/proc").listFiles();
            if (pids != null) {
                for (java.io.File d : pids) {
                    String n = d.getName();
                    if (!n.chars().allMatch(Character::isDigit)) continue;
                    try (BufferedReader br = new BufferedReader(new java.io.FileReader("/proc/" + n + "/cmdline"))) {
                        String line = br.readLine();
                        if (line == null) continue;
                        int z = line.indexOf('\0');
                        if (z >= 0) line = line.substring(0, z);
                        if (line.equals(pkg) || line.startsWith(pkg + ":")) return Integer.parseInt(n);
                    } catch (Throwable ignored) { }
                }
            }
        } catch (Throwable ignored) { }
        return -1;
    }

    // ---------------------------------------------------------------- diag

    static String diagJson() {
        StringBuilder routes = new StringBuilder("{");
        int i = 0;
        for (Map.Entry<String, String> e : ROUTES.entrySet()) {
            if (i++ > 0) routes.append(',');
            routes.append(Out.quote(e.getKey())).append(':').append(Out.quote(e.getValue()));
        }
        routes.append('}');

        StringBuilder notes = new StringBuilder("[");
        for (int k = 0; k < NOTES.size(); k++) {
            if (k > 0) notes.append(',');
            notes.append(Out.quote(NOTES.get(k)));
        }
        notes.append(']');

        return new Out(true)
                .raw("routes", routes.toString())
                .raw("notes", notes.toString())
                .put("uid", Process.myUid())
                .put("sdk", Build.VERSION.SDK_INT)
                .put("atm_startActivity", listSigs("android.app.IActivityTaskManager", "startactivity"))
                .put("atm_getTasks", listSigs("android.app.IActivityTaskManager", "gettasks"))
                .put("am_getServices", listSigs("android.app.IActivityManager", "getservices"))
                .put("am_broadcastIntent", listSigs("android.app.IActivityManager", "broadcastintent"))
                .put("am_forceStopPackage", listSigs("android.app.IActivityManager", "forcestoppackage"))
                .put("am_runningAppProcs", listSigs("android.app.IActivityManager", "getrunningappprocesses"))
                .put("wm_focus", listSigs("android.view.IWindowManager", "focus"))
                .put("clipboard_get", listSigs("android.content.IClipboard", "getprimaryclipx"))
                .put("clipboard_getClip", listSigs("android.content.IClipboard", "getprimaryclip"))
                .put("clipboard_set", listSigs("android.content.IClipboard", "setprimaryclip"))
                .put("pm_getInstalled", listSigs("android.content.pm.IPackageManager", "getinstalledpackages"))
                .put("pm_getPackageInfo", listSigs("android.content.pm.IPackageManager", "getpackageinfox"))
                .put("pm_pkgInfo", listSigs("android.content.pm.IPackageManager", "getpackageinfo"))
                .put("pm_resolve", listSigs("android.content.pm.IPackageManager", "resolveintent"))
                .put("pm_query", listSigs("android.content.pm.IPackageManager", "queryintentactivities"))
                .done();
    }

    // ---------------------------------------------------------------- server

    static void serve(int port) throws Exception {
        ServerSocket ss = new ServerSocket();
        ss.setReuseAddress(true);
        ss.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), port), 8);
        System.out.println("listening 127.0.0.1:" + port);
        System.out.flush();
        while (!sQuit) {
            Socket s = ss.accept();
            if (CONNS.get() >= MAX_CONNS) {
                try { s.close(); } catch (Throwable ignored) { }
                continue;
            }
            CONNS.incrementAndGet();
            Thread t = new Thread(() -> handle(s), "conn");
            t.setDaemon(true);
            t.start();
        }
    }

    static void handle(Socket s) {
        try {
            s.setTcpNoDelay(true);
            BufferedReader in = new BufferedReader(new InputStreamReader(s.getInputStream(), StandardCharsets.UTF_8));
            OutputStream out = s.getOutputStream();
            String line;
            while ((line = in.readLine()) != null) {
                String reply;
                EXEC.lock();                       // one command at a time, daemon-wide
                try {
                    reply = exec(line);
                } catch (Throwable t) {
                    reply = err(t);
                } finally {
                    EXEC.unlock();
                }
                out.write(reply.getBytes(StandardCharsets.UTF_8));
                out.write('\n');
                out.flush();
                if (sQuit) break;
            }
        } catch (Throwable t) {
            // client died; nothing to salvage
        } finally {
            CONNS.decrementAndGet();
            try { s.close(); } catch (Throwable ignored) { }
        }
    }

    static {
        // bootMs is set in boot(); nothing to initialise here.
    }
}
