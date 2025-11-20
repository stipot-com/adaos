package inimatic.loginwv;

import android.os.Build;
import android.app.Dialog;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.CookieManager;
import android.webkit.WebSettings;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.CookieSyncManager;

import org.apache.cordova.*;
import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URI;
import java.net.URISyntaxException;
import java.util.*;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.io.IOException;
import java.net.HttpURLConnection;
import java.net.MalformedURLException;
import java.net.ProtocolException;

public class LoginWV extends CordovaPlugin {

  	private WebView webViewRef;
	private Map<String, Map<String, String>> cookieMap = new LinkedHashMap<>();

  	@Override
  	public boolean execute(String action, JSONArray args, CallbackContext callbackContext) throws JSONException {
    	if ("login".equals(action)) {
			final String url = args.getString(0);
			final String checkLoginJs = args.getString(1);
			login(url, checkLoginJs, callbackContext);
			return true;
    	}
    	return false;
  	}

  	private void login(String loginUrl, String checkLoginJs, CallbackContext callbackContext) {
    	cordova.getActivity().runOnUiThread(() -> {
			LOG.d("LoginWV", "Opening URL: " + loginUrl);
			Dialog dialog = new Dialog(cordova.getContext());
			WebView webView = new WebView(cordova.getContext());
			webViewRef = webView;

			WebSettings settings = webView.getSettings();
			settings.setJavaScriptEnabled(true);
			settings.setDomStorageEnabled(true);

			webView.setWebViewClient(new WebViewClient() {
				@Override
				public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
					try {
						URL url = new URL(request.getUrl().toString());
						HttpURLConnection connection = (HttpURLConnection) url.openConnection();

						connection.setRequestMethod("GET");
						connection.setInstanceFollowRedirects(true);
						connection.connect();

						Map<String, List<String>> headers = connection.getHeaderFields();
						parseCookiesHeaders(cookieMap, headers);

					} catch (MalformedURLException e) {
						LOG.e("WebView", "Malformed URL: " + e.getMessage());
					} catch (ProtocolException e) {
						LOG.e("WebView", "Protocol error: " + e.getMessage());
					} catch (IOException e) {
						LOG.e("WebView", "IO error: " + e.getMessage());
					}

					return null;
				}

				@Override
				public void onPageFinished(WebView view, String url) {
					super.onPageFinished(view, url);

					view.evaluateJavascript(checkLoginJs, result -> {
						if (result != null && result.equals("true")) {
							parseCookieManager(cookieMap, loginUrl);
							String cookies = getCookiesJSON(cookieMap);
							dialog.dismiss();
							webView.destroy();
							callbackContext.success(cookies != null ? cookies : "");
							cookieMap = new LinkedHashMap<>();
						}
					});
				}
			});

			webView.loadUrl(loginUrl);
			dialog.setContentView(webView);
			dialog.show();
			dialog.setOnDismissListener(d -> webView.destroy());
		});
  	}

	public static void parseCookiesHeaders(Map<String, Map<String, String>> cookieMap, Map<String, List<String>> headers) {
		Pattern cookiePattern = Pattern.compile("(.*?);.*?domain=(.*?);.*?path=(.*?);", Pattern.CASE_INSENSITIVE);

		for (Map.Entry<String, List<String>> entry : headers.entrySet()) {
			if ("set-cookie".equalsIgnoreCase(entry.getKey())) {
				for (String cookieHeader : entry.getValue()) {
					Map<String, String> cookieData = new HashMap<>();
					String[] parts = cookieHeader.split(";", -1);
					boolean isFirst = true;
					String name = "";

					for (String part : parts) {
						String[] pair = part.trim().split("=", 2);
						String key = pair[0].trim().toLowerCase();
						String val = pair.length > 1 ? pair[1].trim() : "";

						if (isFirst) {
							cookieData.put("value", val);
							name = pair[0].trim();
							isFirst = false;
						} else {
							if (val.isEmpty()) {
								cookieData.put(key, "true");
							} else {
								cookieData.put(key, val);
							}
						}
					}

					cookieMap.put(name, cookieData);
				}
			}
		}
	}

	public static void parseCookieManager(Map<String, Map<String, String>> cookieMap, String url) {
		String cookieString = CookieManager.getInstance().getCookie(url);
		if (cookieString != null && !cookieString.isEmpty()) {
			for (String cookiePair : cookieString.split(";")) {
				String[] pair = cookiePair.trim().split("=", 2);
				if (pair.length < 2) continue;

				String name = pair[0];
				String value = pair[1];

				if (cookieMap.containsKey(name)) continue;

				Map<String, String> cookieData = new HashMap<>();
				cookieData.put("value", value);
				cookieData.put("domain", "");
				cookieData.put("path", "/");
				cookieData.put("httpOnly", "false");
				cookieData.put("secure", "false");

				cookieMap.put(name, cookieData);
			}
		}
	}

	public static String getCookiesJSON(Map<String, Map<String, String>> cookieMap) {
		JSONObject result = new JSONObject();
		for (Map.Entry<String, Map<String, String>> entry : cookieMap.entrySet()) {
			JSONObject cookieJson = new JSONObject();
			try {
				Map<String, String> data = entry.getValue();
				cookieJson.put("value", data.get("value"));
				cookieJson.put("domain", data.get("domain"));
				cookieJson.put("path", data.get("path"));
				cookieJson.put("httpOnly", data.get("httpOnly"));
				cookieJson.put("secure", data.get("secure"));
				result.put(entry.getKey(), cookieJson);
			} catch (Exception ignored) {}
		}

		return result.toString();
	}

}


