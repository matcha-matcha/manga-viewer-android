package jp.mangaviewer.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.DialogInterface;
import android.content.Intent;
import android.content.pm.PackageInfo;
import android.net.Uri;
import android.widget.Toast;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.text.InputType;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.HttpURLConnection;
import java.net.InterfaceAddress;
import java.net.NetworkInterface;
import java.net.Socket;
import java.net.URL;
import java.util.ArrayList;
import java.util.Enumeration;
import java.util.List;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

/**
 * まんがビューワー Android アプリ。
 * ・初回: LAN内のまんがビューワーサーバーを自動探索して一覧表示 → タップで選択 → 記憶
 * ・2回目以降: 記憶したサーバーへ直接接続。つながらない場合(IP変更/PC入れ替え)は自動で探し直す
 * ・全画面(没入モード)のWebViewでサーバーのUIを表示
 */
public class MainActivity extends Activity {
    static final int PORT = 8765;

    private SharedPreferences sp;
    private final Handler ui = new Handler(Looper.getMainLooper());
    private FrameLayout root;
    private WebView web;
    private LinearLayout panel;
    private TextView title;
    private TextView status;
    private LinearLayout list;
    private volatile int scanGen = 0;
    private String baseUrl = null;

    static class Srv {
        String name;
        String id;
        String url;
    }

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        sp = getSharedPreferences("mv", MODE_PRIVATE);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        if (Build.VERSION.SDK_INT >= 28) {
            WindowManager.LayoutParams lp = getWindow().getAttributes();
            lp.layoutInDisplayCutoutMode = WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES;
            getWindow().setAttributes(lp);
        }
        buildUi();
        setContentView(root);

        checkUpdate(false);          // 起動時に自動で更新確認(6時間に1回まで)
        String saved = sp.getString("url", null);
        if (saved == null) {
            startDiscovery(null);
        } else {
            connectSaved(saved);
        }
    }

    // ------------------------------------------------------------ UI構築
    private int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density + 0.5f);
    }

    private Button mkButton(String text) {
        Button b = new Button(this);
        b.setText(text);
        b.setAllCaps(false);
        b.setTextColor(Color.WHITE);
        b.setBackgroundColor(Color.parseColor("#2a2e36"));
        b.setPadding(dp(14), dp(14), dp(14), dp(14));
        return b;
    }

    private void buildUi() {
        root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);

        web = new WebView(this);
        web.setBackgroundColor(Color.BLACK);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setSupportZoom(true);
        s.setBuiltInZoomControls(true);
        s.setDisplayZoomControls(false);
        s.setMediaPlaybackRequiresUserGesture(false);
        web.addJavascriptInterface(new Bridge(), "MangaApp");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request != null && request.isForMainFrame()) {
                    ui.post(new Runnable() {
                        @Override
                        public void run() {
                            onServerLost();
                        }
                    });
                }
            }
        });
        web.setVisibility(View.GONE);
        root.addView(web, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

        panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setBackgroundColor(Color.parseColor("#14161a"));
        panel.setPadding(dp(20), dp(40), dp(20), dp(20));

        title = new TextView(this);
        title.setText("📚 まんがビューワー");
        title.setTextColor(Color.WHITE);
        title.setTextSize(24);
        panel.addView(title);

        status = new TextView(this);
        status.setTextColor(Color.parseColor("#9aa0a6"));
        status.setTextSize(15);
        status.setPadding(0, dp(8), 0, dp(12));
        panel.addView(status);

        ScrollView sv = new ScrollView(this);
        list = new LinearLayout(this);
        list.setOrientation(LinearLayout.VERTICAL);
        sv.addView(list);
        panel.addView(sv, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER);
        Button rescan = mkButton("🔄 再検索");
        rescan.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                startDiscovery(null);
            }
        });
        Button manual = mkButton("⌨ 手入力");
        manual.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                askManual();
            }
        });
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
        lp.setMargins(dp(4), dp(8), dp(4), 0);
        row.addView(rescan, lp);
        row.addView(manual, lp);
        panel.addView(row);

        root.addView(panel, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
    }

    private void showPanel() {
        panel.setVisibility(View.VISIBLE);
    }

    private void hidePanel() {
        panel.setVisibility(View.GONE);
    }

    // ------------------------------------------------------------ 全画面(没入モード)
    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) {
            getWindow().getDecorView().setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                            | View.SYSTEM_UI_FLAG_FULLSCREEN
                            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                            | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                            | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
        }
    }

    @Override
    public void onBackPressed() {
        if (panel.getVisibility() == View.VISIBLE && baseUrl != null) {
            hidePanel();
        } else if (web.getVisibility() == View.VISIBLE && web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    // ------------------------------------------------------------ 接続
    private void load(Srv s) {
        sp.edit().putString("url", s.url).putString("id", s.id == null ? "" : s.id).putString("name", s.name == null ? "" : s.name).apply();
        loadUrl(s.url);
    }

    private void loadUrl(String url) {
        baseUrl = url;
        hidePanel();
        web.setVisibility(View.VISIBLE);
        web.loadUrl(url);
    }

    /** 記憶したサーバーへ接続。ダメなら同じサーバー(ID一致)を探し直す */
    private void connectSaved(final String saved) {
        status.setText("サーバーに接続中…");
        showPanel();
        new Thread() {
            @Override
            public void run() {
                final Srv ok = probeUrl(saved);
                if (ok != null) {
                    ui.post(new Runnable() {
                        @Override
                        public void run() {
                            loadUrl(saved);
                        }
                    });
                } else {
                    ui.post(new Runnable() {
                        @Override
                        public void run() {
                            startDiscovery("前回のサーバーに接続できません。探し直します…");
                        }
                    });
                }
            }
        }.start();
    }

    private void onServerLost() {
        web.setVisibility(View.GONE);
        baseUrl = null;
        startDiscovery("サーバーに接続できません。探し直します…");
    }

    private void askManual() {
        final EditText et = new EditText(this);
        et.setHint("例 192.168.1.20");
        et.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        new AlertDialog.Builder(this)
                .setTitle("サーバーのアドレス")
                .setView(et)
                .setPositiveButton("接続", new DialogInterface.OnClickListener() {
                    @Override
                    public void onClick(DialogInterface d, int w) {
                        String v = et.getText().toString().trim();
                        if (v.length() == 0) return;
                        if (!v.startsWith("http")) v = "http://" + v;
                        if (v.indexOf(':', 7) < 0) v = v + ":" + PORT;
                        if (!v.endsWith("/")) v = v + "/";
                        final String url = v;
                        status.setText("接続を確認中…");
                        new Thread() {
                            @Override
                            public void run() {
                                final Srv s = probeUrl(url);
                                ui.post(new Runnable() {
                                    @Override
                                    public void run() {
                                        if (s != null) load(s);
                                        else status.setText("接続できませんでした: " + url);
                                    }
                                });
                            }
                        }.start();
                    }
                })
                .setNegativeButton("キャンセル", null)
                .show();
    }

    // ------------------------------------------------------------ 探索
    private void startDiscovery(String message) {
        final int gen = ++scanGen;
        showPanel();
        status.setText(message != null ? message : "ネットワークを探しています…");
        list.removeAllViews();
        new Thread() {
            @Override
            public void run() {
                final List<Srv> found = scan();
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        if (gen != scanGen) return;
                        showResults(found);
                    }
                });
            }
        }.start();
    }

    private void showResults(List<Srv> found) {
        list.removeAllViews();
        String savedId = sp.getString("id", "");
        // 同じサーバー(ID一致)が見つかれば自動接続(IP変更など)
        if (savedId.length() > 0) {
            for (Srv s : found) {
                if (savedId.equals(s.id)) {
                    load(s);
                    return;
                }
            }
        }
        // 1台だけなら自動接続
        if (found.size() == 1) {
            load(found.get(0));
            return;
        }
        if (found.isEmpty()) {
            status.setText("サーバーが見つかりません。サーバーPCでアプリが起動しているか、同じWi-Fiか確認してください。");
            return;
        }
        status.setText(found.size() + " 台見つかりました。タップして選択");
        for (final Srv s : found) {
            Button b = mkButton("🖥  " + s.name + "\n" + s.url);
            b.setGravity(Gravity.START | Gravity.CENTER_VERTICAL);
            b.setOnClickListener(new View.OnClickListener() {
                @Override
                public void onClick(View v) {
                    load(s);
                }
            });
            LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
            lp.setMargins(0, dp(6), 0, dp(6));
            list.addView(b, lp);
        }
    }

    private List<String> localPrefixes() {
        List<String> out = new ArrayList<String>();
        try {
            Enumeration<NetworkInterface> en = NetworkInterface.getNetworkInterfaces();
            while (en != null && en.hasMoreElements()) {
                NetworkInterface ni = en.nextElement();
                if (!ni.isUp() || ni.isLoopback()) continue;
                for (InterfaceAddress ia : ni.getInterfaceAddresses()) {
                    InetAddress a = ia.getAddress();
                    if (a instanceof Inet4Address && a.isSiteLocalAddress()) {
                        String ip = a.getHostAddress();
                        String p = ip.substring(0, ip.lastIndexOf('.'));
                        if (!out.contains(p)) out.add(p);
                    }
                }
            }
        } catch (Exception ignored) {
        }
        return out;
    }

    private List<Srv> scan() {
        List<Srv> result = new ArrayList<Srv>();
        List<Callable<Srv>> tasks = new ArrayList<Callable<Srv>>();
        for (String p : localPrefixes()) {
            for (int i = 1; i < 255; i++) {
                final String ip = p + "." + i;
                tasks.add(new Callable<Srv>() {
                    @Override
                    public Srv call() {
                        return probeHost(ip);
                    }
                });
            }
        }
        ExecutorService pool = Executors.newFixedThreadPool(64);
        try {
            List<Future<Srv>> fs = pool.invokeAll(tasks, 10, TimeUnit.SECONDS);
            for (Future<Srv> f : fs) {
                try {
                    Srv s = f.get();
                    if (s != null) result.add(s);
                } catch (Exception ignored) {
                }
            }
        } catch (Exception ignored) {
        } finally {
            pool.shutdownNow();
        }
        return result;
    }

    private Srv probeHost(String ip) {
        Socket sock = new Socket();
        try {
            sock.connect(new InetSocketAddress(ip, PORT), 400);
        } catch (Exception e) {
            return null;
        } finally {
            try {
                sock.close();
            } catch (Exception ignored) {
            }
        }
        return probeUrl("http://" + ip + ":" + PORT + "/");
    }

    /** /api/status を叩いてまんがビューワーのサーバーか確認 */
    private Srv probeUrl(String base) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(base + "api/status").openConnection();
            c.setConnectTimeout(2000);
            c.setReadTimeout(2500);
            if (c.getResponseCode() != 200) return null;
            BufferedReader br = new BufferedReader(new InputStreamReader(c.getInputStream(), "UTF-8"));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = br.readLine()) != null) sb.append(line);
            br.close();
            JSONObject j = new JSONObject(sb.toString());
            if (!j.has("configured")) return null;
            Srv s = new Srv();
            s.url = base;
            s.name = j.optString("name", base);
            s.id = j.optString("id", "");
            return s;
        } catch (Exception e) {
            return null;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    // ------------------------------------------------------------ アプリ自身の更新
    static final String REPO = "matcha-matcha/manga-viewer-android";

    private int myBuild() {
        try {
            PackageInfo pi = getPackageManager().getPackageInfo(getPackageName(), 0);
            return pi.versionCode;
        } catch (Exception e) {
            return 0;
        }
    }

    /** GitHub の最新リリース(build-N)を調べ、新しければ「更新しますか?」を出す。manual=true は結果を必ず表示 */
    private void checkUpdate(final boolean manual) {
        long now = System.currentTimeMillis();
        if (!manual && now - sp.getLong("upd_t", 0) < 6L * 3600 * 1000) return;
        sp.edit().putLong("upd_t", now).apply();
        new Thread(new Runnable() {
            @Override
            public void run() {
                int best = -1;
                String url = null;
                String err = null;
                HttpURLConnection c = null;
                try {
                    c = (HttpURLConnection) new URL("https://api.github.com/repos/" + REPO + "/releases?per_page=30").openConnection();
                    c.setConnectTimeout(6000);
                    c.setReadTimeout(8000);
                    c.setRequestProperty("User-Agent", "MangaViewer");
                    c.setRequestProperty("Accept", "application/vnd.github+json");
                    if (c.getResponseCode() != 200) {
                        err = "HTTP " + c.getResponseCode();
                    } else {
                        BufferedReader br = new BufferedReader(new InputStreamReader(c.getInputStream(), "UTF-8"));
                        StringBuilder sb = new StringBuilder();
                        String ln;
                        while ((ln = br.readLine()) != null) sb.append(ln);
                        br.close();
                        JSONArray arr = new JSONArray(sb.toString());
                        for (int i = 0; i < arr.length(); i++) {
                            JSONObject r = arr.getJSONObject(i);
                            String tag = r.optString("tag_name", "");
                            if (!tag.startsWith("build-")) continue;
                            int n;
                            try {
                                n = Integer.parseInt(tag.substring(6));
                            } catch (Exception e) {
                                continue;
                            }
                            JSONArray as = r.optJSONArray("assets");
                            if (as == null || n <= best) continue;
                            for (int k = 0; k < as.length(); k++) {
                                JSONObject a = as.getJSONObject(k);
                                if (a.optString("name", "").endsWith(".apk")) {
                                    best = n;
                                    url = a.optString("browser_download_url", null);
                                }
                            }
                        }
                    }
                } catch (Exception e) {
                    err = String.valueOf(e.getMessage());
                } finally {
                    if (c != null) c.disconnect();
                }
                final int fb = best;
                final String fu = url;
                final String fe = err;
                ui.post(new Runnable() {
                    @Override
                    public void run() {
                        if (isFinishing()) return;
                        int mine = myBuild();
                        if (fe != null || fu == null) {
                            if (manual) Toast.makeText(MainActivity.this, "更新を確認できません (" + (fe == null ? "なし" : fe) + ")", Toast.LENGTH_LONG).show();
                        } else if (fb > mine) {
                            new AlertDialog.Builder(MainActivity.this)
                                    .setTitle("アプリの更新があります")
                                    .setMessage("新しいバージョン (build " + fb + ") があります。\n今のバージョン: build " + mine + "\n\n「更新」を押すとダウンロードが始まります。終わったら通知かダウンロード一覧のファイルをタップしてインストールしてください。")
                                    .setPositiveButton("更新", new DialogInterface.OnClickListener() {
                                        @Override
                                        public void onClick(DialogInterface d, int w) {
                                            try {
                                                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(fu)));
                                            } catch (Exception e) {
                                                Toast.makeText(MainActivity.this, "ブラウザを開けません", Toast.LENGTH_LONG).show();
                                            }
                                        }
                                    })
                                    .setNegativeButton("あとで", null)
                                    .show();
                        } else if (manual) {
                            Toast.makeText(MainActivity.this, "最新版です (build " + mine + ")", Toast.LENGTH_LONG).show();
                        }
                    }
                });
            }
        }).start();
    }

    // ------------------------------------------------------------ Web → アプリ連携
    private class Bridge {
        @JavascriptInterface
        public boolean isApp() {
            return true;
        }

        @JavascriptInterface
        public int appBuild() {
            return myBuild();
        }

        @JavascriptInterface
        public void checkUpdate() {
            ui.post(new Runnable() {
                @Override
                public void run() {
                    MainActivity.this.checkUpdate(true);
                }
            });
        }

        @JavascriptInterface
        public void changeServer() {
            ui.post(new Runnable() {
                @Override
                public void run() {
                    startDiscovery(null);
                }
            });
        }
    }
}
