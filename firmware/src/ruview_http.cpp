#include "ruview_shared.h"
#include <LittleFS.h>
#include <ArduinoJson.h>

RuViewConfig g_cfg;
AsyncWebServer g_server(80);
AsyncWebSocket g_ws("/ws");
Preferences g_prefs;

void ruviewLoadConfig() {
  memset(&g_cfg, 0, sizeof(g_cfg));
  g_cfg.server_port = RUVIEW_UDP_PORT_DEFAULT;
  g_cfg.traffic_hz = RUVIEW_TRAFFIC_HZ_DEFAULT;
  g_cfg.beacon_hz = RUVIEW_BEACON_HZ_DEFAULT;
  g_cfg.ssid[0] = '\0';
  g_cfg.provisioned = false;

  if (!g_prefs.begin("ruview", true)) {
    return;
  }
  String s = g_prefs.getString("ssid", "");
  String p = g_prefs.getString("pass", "");
  String srv = g_prefs.getString("srv", "");
  uint16_t port = (uint16_t)g_prefs.getUInt("port", RUVIEW_UDP_PORT_DEFAULT);
  uint16_t adrp = (uint16_t)g_prefs.getUInt("adrp", 0);
  uint16_t thz = (uint16_t)g_prefs.getUInt("thz", RUVIEW_TRAFFIC_HZ_DEFAULT);
  uint16_t bhz = (uint16_t)g_prefs.getUInt("bhz", RUVIEW_BEACON_HZ_DEFAULT);
  /* Нет ключа "ok" — настройки от старой прошивки, они уже работали: считаем проверенными. */
  bool okOnce = g_prefs.isKey("ok") ? g_prefs.getBool("ok", false) : s.length() > 0;
  g_prefs.end();

  if (s.length() > 0) {
    strlcpy(g_cfg.ssid, s.c_str(), sizeof(g_cfg.ssid));
    strlcpy(g_cfg.pass, p.c_str(), sizeof(g_cfg.pass));
    strlcpy(g_cfg.server_ip, srv.c_str(), sizeof(g_cfg.server_ip));
    g_cfg.server_port = port;
    g_cfg.ruview_adr_port = adrp;
    g_cfg.traffic_hz = thz;
    g_cfg.beacon_hz = bhz;
    g_cfg.connected_once = okOnce;
    g_cfg.provisioned = true;
  }
}

void ruviewSaveConfig() {
  g_prefs.begin("ruview", false);
  g_prefs.putString("ssid", g_cfg.ssid);
  g_prefs.putString("pass", g_cfg.pass);
  g_prefs.putString("srv", g_cfg.server_ip);
  g_prefs.putUInt("port", g_cfg.server_port);
  g_prefs.putUInt("adrp", g_cfg.ruview_adr_port);
  g_prefs.putUInt("thz", g_cfg.traffic_hz);
  g_prefs.putUInt("bhz", g_cfg.beacon_hz);
  g_prefs.putBool("ok", g_cfg.connected_once);
  g_prefs.end();
}

void ruviewClearConfig() {
  g_prefs.begin("ruview", false);
  g_prefs.clear();
  g_prefs.end();
}

static void onWsEvent(AsyncWebSocket *server, AsyncWebSocketClient *client, AwsEventType type, void *arg,
                      uint8_t *data, size_t len) {
  (void)server;
  (void)client;
  (void)type;
  (void)arg;
  (void)data;
  (void)len;
}

static void handleSettingsBody(AsyncWebServerRequest *request, uint8_t *data, size_t len, size_t index,
                               size_t total) {
  static String body;
  if (index == 0) {
    body = "";
    body.reserve(total);
  }
  for (size_t i = 0; i < len; i++) {
    body += (char)data[i];
  }
  if (index + len < total) {
    return;
  }

  JsonDocument doc;
  if (deserializeJson(doc, body)) {
    request->send(400, "application/json", "{\"ok\":false,\"err\":\"json\"}");
    return;
  }
  if (!doc["server_ip"].isNull()) {
    const char *srv = doc["server_ip"] | "";
    strlcpy(g_cfg.server_ip, srv, sizeof(g_cfg.server_ip));
  }
  if (!doc["server_port"].isNull()) {
    g_cfg.server_port = (uint16_t)(doc["server_port"] | (int)RUVIEW_UDP_PORT_DEFAULT);
  }
  if (!doc["ruview_adr_port"].isNull()) {
    g_cfg.ruview_adr_port = (uint16_t)(doc["ruview_adr_port"] | 0);
  }
  bool trafficChanged = false;
  if (!doc["traffic_hz"].isNull()) {
    int hz = doc["traffic_hz"] | (int)RUVIEW_TRAFFIC_HZ_DEFAULT;
    hz = hz < 0 ? 0 : (hz > 500 ? 500 : hz);
    trafficChanged = (uint16_t)hz != g_cfg.traffic_hz;
    g_cfg.traffic_hz = (uint16_t)hz;
  }
  if (!doc["beacon_hz"].isNull()) {
    int hz = doc["beacon_hz"] | (int)RUVIEW_BEACON_HZ_DEFAULT;
    g_cfg.beacon_hz = (uint16_t)(hz < 0 ? 0 : (hz > 500 ? 500 : hz)); /* задача маяков подхватит сама */
  }

  g_prefs.begin("ruview", false);
  g_prefs.putString("srv", g_cfg.server_ip);
  g_prefs.putUInt("port", g_cfg.server_port);
  g_prefs.putUInt("adrp", g_cfg.ruview_adr_port);
  g_prefs.putUInt("thz", g_cfg.traffic_hz);
  g_prefs.putUInt("bhz", g_cfg.beacon_hz);
  g_prefs.end();
  if (trafficChanged) {
    ruviewTrafficRestart();
  }

  request->send(200, "application/json", "{\"ok\":true}");
}

static bool s_apMode = false;

static void handleProvisionBody(AsyncWebServerRequest *request, uint8_t *data, size_t len, size_t index,
                                size_t total) {
  static String body;
  if (index == 0) {
    body = "";
    body.reserve(total);
  }
  for (size_t i = 0; i < len; i++) {
    body += (char)data[i];
  }
  if (index + len < total) {
    return;
  }
  /* В рабочем режиме смена Wi-Fi по сети запрещена: только через RuView_Setup (BOOT при включении). */
  if (!s_apMode) {
    request->send(403, "application/json", "{\"ok\":false,\"err\":\"provision only in setup mode\"}");
    return;
  }

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, body);
  if (err) {
    request->send(400, "application/json", "{\"ok\":false,\"err\":\"json\"}");
    return;
  }
  const char *ssid = doc["ssid"] | "";
  const char *pass = doc["password"] | doc["pass"] | "";
  const char *srv = doc["server_ip"] | doc["server"] | "";
  uint16_t port = (uint16_t)(doc["server_port"] | (int)RUVIEW_UDP_PORT_DEFAULT);

  if (strlen(ssid) == 0) {
    request->send(400, "application/json", "{\"ok\":false,\"err\":\"ssid\"}");
    return;
  }

  strlcpy(g_cfg.ssid, ssid, sizeof(g_cfg.ssid));
  strlcpy(g_cfg.pass, pass, sizeof(g_cfg.pass));
  strlcpy(g_cfg.server_ip, srv, sizeof(g_cfg.server_ip));
  g_cfg.server_port = port;
  g_cfg.ruview_adr_port = (uint16_t)(doc["ruview_adr_port"] | 0);
  g_cfg.connected_once = false;
  g_cfg.provisioned = true;
  ruviewSaveConfig();

  request->send(200, "application/json", "{\"ok\":true,\"reboot\":true}");
  ruviewRequestRestart(800);
}

void ruviewHttpBegin(bool apMode) {
  s_apMode = apMode;
  g_ws.onEvent(onWsEvent);
  g_server.addHandler(&g_ws);

  /* Метка раздела = первое поле в partitions_ruview.csv (у нас «storage», не «spiffs»). */
  if (!LittleFS.begin(true, "/littlefs", 10, "storage")) {
    Serial.println(F("[RuView] LittleFS mount failed — загрузите файловую систему: pio run -t uploadfs"));
  }

  g_server.serveStatic("/", LittleFS, "/").setDefaultFile("index.html");

  g_server.on("/api/status", HTTP_GET, [apMode](AsyncWebServerRequest *request) {
    JsonDocument doc;
    doc["mode"] = apMode ? "ap" : "sta";
    doc["heap"] = ESP.getFreeHeap();
    doc["psram"] = ESP.getFreePsram();
    doc["udp_server"] = g_cfg.server_ip;
    doc["udp_port"] = g_cfg.server_port;
    doc["ruview_adr_port"] = g_cfg.ruview_adr_port;
    if (apMode) {
      doc["ap_ip"] = WiFi.softAPIP().toString();
    } else {
      doc["ip"] = WiFi.localIP().toString();
      doc["rssi"] = WiFi.RSSI();
      doc["channel"] = WiFi.channel();
      ruviewCsiFillStatus(doc);
    }
    String j;
    serializeJson(doc, j);
    request->send(200, "application/json", j);
  });

  g_server.on("/api/calibrate", HTTP_POST, [](AsyncWebServerRequest *request) {
    ruviewRequestCalibrate();
    request->send(200, "application/json", "{\"ok\":true}");
  });

  g_server.on("/api/provision", HTTP_POST, [](AsyncWebServerRequest *request) {}, NULL, handleProvisionBody);

  g_server.on("/api/settings", HTTP_POST, [](AsyncWebServerRequest *request) {}, NULL, handleSettingsBody);

  g_server.on("/generate_204", HTTP_GET, [](AsyncWebServerRequest *request) { request->redirect("/"); });
  g_server.on("/hotspot-detect.html", HTTP_GET, [](AsyncWebServerRequest *request) { request->redirect("/"); });
  g_server.on("/ncsi.txt", HTTP_GET, [](AsyncWebServerRequest *request) { request->redirect("/"); });
  g_server.on("/connecttest.txt", HTTP_GET, [](AsyncWebServerRequest *request) { request->redirect("/"); });

  g_server.onNotFound([apMode](AsyncWebServerRequest *request) {
    if (apMode && request->method() == HTTP_GET) {
      String url = "http://" + WiFi.softAPIP().toString() + "/";
      request->redirect(url);
      return;
    }
    request->send(404, "text/plain", "Not found");
  });

  g_server.begin();
}
