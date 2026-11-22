/**
 * RuView Ultimate — ESP32-S3 Wi-Fi CSI
 * STA: EU country, CSI от роутера (свой ping-трафик) и от других узлов (маяки ESP-NOW) → UDP :5555 + WebSocket
 * AP: RuView_Setup + captive portal для ввода SSID/пароля
 */
#include "ruview_shared.h"
#include <DNSServer.h>
#include <ESPmDNS.h>
#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_system.h>
#include <math.h>

static DNSServer g_dns;
static bool g_apMode = false;
static TaskHandle_t g_streamTask = nullptr;
static bool g_serialWasOpen = false;
static bool g_mdnsStarted = false;
static volatile bool g_gotIpPending = false;
static uint32_t g_restartAtMs = 0;
static uint32_t g_lastConnectAttemptMs = 0;
static volatile uint32_t g_disconnectCount = 0;
static volatile uint32_t g_gotIpCount = 0;
static volatile uint32_t g_lastDisconnectMs = 0;
static volatile uint8_t g_lastDisconnectReason = 0;
static uint32_t g_reconnectAttempts = 0;
static esp_err_t g_bandwidthSetError = ESP_OK;
static float g_chipTemperatureC = NAN;
static uint32_t g_temperatureAtMs = 0;
static volatile bool g_disconnectLogPending = false;
struct DisconnectRecord { uint32_t atMs; uint8_t reason; };
static DisconnectRecord g_disconnectHistory[8] = {};
static portMUX_TYPE g_wifiDiagMux = portMUX_INITIALIZER_UNLOCKED;

static const char *resetReasonName(esp_reset_reason_t reason) {
  switch (reason) {
    case ESP_RST_POWERON: return "power_on";
    case ESP_RST_EXT: return "external";
    case ESP_RST_SW: return "software";
    case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: return "interrupt_watchdog";
    case ESP_RST_TASK_WDT: return "task_watchdog";
    case ESP_RST_WDT: return "watchdog";
    case ESP_RST_DEEPSLEEP: return "deep_sleep";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_SDIO: return "sdio";
    default: return "unknown";
  }
}

void ruviewWifiFillStatus(JsonDocument &doc) {
  doc["firmware"] = "network-diag-2";
  doc["uptime_ms"] = millis();
  doc["reset_reason"] = (int)esp_reset_reason();
  doc["reset_reason_name"] = resetReasonName(esp_reset_reason());
  doc["min_heap"] = ESP.getMinFreeHeap();
  if (isfinite(g_chipTemperatureC)) doc["chip_temperature_c"] = g_chipTemperatureC;
  else doc["chip_temperature_c"] = nullptr;
  doc["temperature_age_ms"] = g_temperatureAtMs ? millis() - g_temperatureAtMs : 0;

  uint32_t count, gotIp, at; uint8_t reason;
  DisconnectRecord history[8];
  portENTER_CRITICAL(&g_wifiDiagMux);
  count = g_disconnectCount; gotIp = g_gotIpCount;
  at = g_lastDisconnectMs; reason = g_lastDisconnectReason;
  memcpy(history, g_disconnectHistory, sizeof(history));
  portEXIT_CRITICAL(&g_wifiDiagMux);
  JsonObject wifi = doc["wifi"].to<JsonObject>();
  wifi["status"] = (int)WiFi.status();
  wifi["disconnect_count"] = count;
  wifi["got_ip_count"] = gotIp;
  wifi["reconnect_attempts"] = g_reconnectAttempts;
  wifi["last_disconnect_reason"] = reason;
  wifi["last_disconnect_at_ms"] = at;
  wifi["bandwidth_set_error"] = (int)g_bandwidthSetError;
  wifi_bandwidth_t bandwidth = WIFI_BW_HT20;
  esp_err_t bandwidthError = esp_wifi_get_bandwidth(WIFI_IF_STA, &bandwidth);
  wifi["bandwidth_query_error"] = (int)bandwidthError;
  if (bandwidthError == ESP_OK) wifi["bandwidth_mhz"] = bandwidth == WIFI_BW_HT20 ? 20 : 40;
  else wifi["bandwidth_mhz"] = nullptr;
  uint8_t primary = 0; wifi_second_chan_t secondary = WIFI_SECOND_CHAN_NONE;
  if (esp_wifi_get_channel(&primary, &secondary) == ESP_OK) {
    wifi["primary_channel"] = primary;
    wifi["secondary_channel"] = (int)secondary;
  }
  JsonArray events = wifi["disconnect_history"].to<JsonArray>();
  const uint32_t size = count < 8 ? count : 8;
  for (uint32_t i = 0; i < size; i++) {
    const DisconnectRecord &record = history[(count - size + i) % 8];
    JsonObject event = events.add<JsonObject>();
    event["at_ms"] = record.atMs; event["reason"] = record.reason;
  }
}

/** Сколько ждать первого подключения с новыми (ни разу не проверенными) SSID/паролем, прежде чем вернуться в AP. */
static const uint32_t kFirstConnectTimeoutMs = 30000;
/** Если связь пропала и автопереподключение не справилось — повторяем WiFi.begin() с таким интервалом. */
static const uint32_t kReconnectIntervalMs = 15000;
/** Сколько держать BOOT для сброса Wi-Fi. */
static const uint32_t kResetHoldMs = 5000;

void ruviewRequestRestart(uint32_t delayMs) {
  g_restartAtMs = millis() + delayMs;
  if (g_restartAtMs == 0) {
    g_restartAtMs = 1;
  }
}

static void printRuviewStatus() {
  if (!Serial) {
    return;
  }
  Serial.println();
  Serial.println(F("--- RuView (USB CDC) ---"));
  Serial.printf("Heap free: %u\n", (unsigned)ESP.getFreeHeap());
  Serial.printf("Reset reason: %s; uptime %u ms\n", resetReasonName(esp_reset_reason()), (unsigned)millis());
  if (g_apMode) {
    Serial.printf("Mode: AP \"%s\"\n", RUVIEW_AP_SSID);
    Serial.printf("Open in browser: http://%s/\n", WiFi.softAPIP().toString().c_str());
  } else if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("Mode: STA, узел %s\n", ruviewNodeId().c_str());
    Serial.printf("IP:       http://%s/\n", WiFi.localIP().toString().c_str());
    Serial.printf("mDNS:     http://%s.local/  (если ОС поддерживает)\n", RUVIEW_HOSTNAME);
    Serial.printf("Router:   %s  ch %d  RSSI %d\n", WiFi.BSSIDstr().c_str(), WiFi.channel(), WiFi.RSSI());
    Serial.printf("UDP  →    %s:%u\n", g_cfg.server_ip[0] ? g_cfg.server_ip : "(не задан)", g_cfg.server_port);
  } else {
    Serial.printf("Mode: STA, нет связи с \"%s\" — переподключаюсь\n", g_cfg.ssid);
  }
  Serial.println(F("Сброс Wi-Fi: держать BOOT 5 с во время работы"));
  Serial.println(F("-------------------------"));
  Serial.flush();
}

static void applyEuCountry() {
  esp_err_t e = esp_wifi_set_country_code("EU", true);
  if (e != ESP_OK) {
    wifi_country_t c = {};
    c.cc[0] = 'E';
    c.cc[1] = 'U';
    c.cc[2] = '\0';
    c.schan = 1;
    c.nchan = 13;
    c.max_tx_power = 20;
    c.policy = WIFI_COUNTRY_POLICY_AUTO;
    esp_wifi_set_country(&c);
  }
}

/*
 * Сброс Wi-Fi — долгое нажатие BOOT во время работы. Зажатая в момент включения или RESET,
 * кнопка BOOT (GPIO0) переводит чип в режим загрузки прошивки, и программа вообще не стартует.
 */
static void pollResetButton(uint32_t now) {
  static uint32_t pressedAt = 0;
  static uint32_t reportedSec = 0;
  if (digitalRead(RUVIEW_RESET_PIN) != LOW) {
    if (pressedAt) {
      Serial.println(F("[RuView] BOOT отпущена — сброс отменён"));
    }
    pressedAt = 0;
    return;
  }
  if (!pressedAt) {
    pressedAt = now ? now : 1;
    reportedSec = 0;
    Serial.printf("[RuView] BOOT нажата — держите %u с для сброса Wi-Fi\n", (unsigned)(kResetHoldMs / 1000));
    return;
  }
  const uint32_t held = now - pressedAt;
  if (held >= kResetHoldMs) {
    Serial.println(F("[RuView] Сброс настроек Wi-Fi → RuView_Setup"));
    Serial.flush();
    ruviewClearConfig();
    delay(200);
    ESP.restart();
  }
  const uint32_t sec = held / 1000;
  if (sec != reportedSec) {
    reportedSec = sec;
    Serial.printf("[RuView] сброс через %u с\n", (unsigned)((kResetHoldMs - held + 999) / 1000));
  }
}

static void startApProvisioning() {
  g_apMode = true;
  WiFi.mode(WIFI_AP);
  applyEuCountry();
  const char *pw = nullptr;
  if (!WiFi.softAP(RUVIEW_AP_SSID, pw, 6)) {
    Serial.println(F("[RuView] softAP failed"));
  }
  IPAddress apIP(192, 168, 4, 1);
  WiFi.softAPConfig(apIP, apIP, IPAddress(255, 255, 255, 0));
  g_dns.start(53, "*", apIP);
  Serial.printf("[RuView] AP %s IP %s\n", RUVIEW_AP_SSID, WiFi.softAPIP().toString().c_str());
  ruviewHttpBegin(true);
}

static void onWifiEvent(WiFiEvent_t event, WiFiEventInfo_t info) {
  if (event == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
    portENTER_CRITICAL(&g_wifiDiagMux);
    g_gotIpCount++;
    portEXIT_CRITICAL(&g_wifiDiagMux);
    g_gotIpPending = true;
  } else if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
    const uint32_t at = millis();
    const uint8_t reason = info.wifi_sta_disconnected.reason;
    portENTER_CRITICAL(&g_wifiDiagMux);
    g_disconnectHistory[g_disconnectCount % 8] = {at, reason};
    g_disconnectCount++;
    g_lastDisconnectMs = at;
    g_lastDisconnectReason = reason;
    portEXIT_CRITICAL(&g_wifiDiagMux);
    g_disconnectLogPending = true;
  }
}

/** Вызывается из loop() после получения IP: привязка CSI к роутеру, трафик, mDNS. */
static void handleGotIp() {
  ruviewCsiSetApMac(WiFi.BSSID());
  ruviewTrafficRestart();
  ruviewBeaconBegin();

  if (!g_cfg.connected_once) {
    g_cfg.connected_once = true;
    ruviewSaveConfig();
    Serial.println(F("[RuView] Подключение проверено — настройки Wi-Fi закреплены"));
  }
  if (!g_mdnsStarted && MDNS.begin(RUVIEW_HOSTNAME)) {
    MDNS.addService("http", "tcp", 80);
    MDNS.addServiceTxt("http", "tcp", "board", "esp32-s3");
    MDNS.addServiceTxt("http", "tcp", "app", "ruview");
    g_mdnsStarted = true;
  }
  printRuviewStatus();
}

static void startSta() {
  g_apMode = false;
  WiFi.onEvent(onWifiEvent);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  applyEuCountry();
  esp_wifi_set_ps(WIFI_PS_NONE);
  g_bandwidthSetError = esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20);

  WiFi.begin(g_cfg.ssid, g_cfg.pass);
  g_lastConnectAttemptMs = millis();
  Serial.printf("[RuView] Connecting to \"%s\" ...\n", g_cfg.ssid);

  if (!g_cfg.connected_once) {
    /* Новые данные ни разу не срабатывали — вероятна опечатка в пароле. Ждём и при неудаче возвращаемся в AP. */
    uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < kFirstConnectTimeoutMs) {
      delay(250);
      Serial.print('.');
    }
    Serial.println();
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println(F("[RuView] Не удалось подключиться с новыми данными — снова RuView_Setup"));
      ruviewClearConfig();
      delay(300);
      ESP.restart();
      return;
    }
  }
  /* Проверенные ранее данные никогда не стираем: роутер мог просто перезагружаться. */

  ruviewHttpBegin(false);
  ruviewCsiBegin();

  if (!g_streamTask) {
    xTaskCreatePinnedToCore(ruviewStreamTask, "ruv_stream", 12288, nullptr, 4, &g_streamTask, 1);
  }
}

void setup() {
  Serial.begin(115200);
#if ARDUINO_USB_CDC_ON_BOOT
  /* Пока ПК не открыл /dev/ttyACM*, USB-CDC может выбросить первые строки — даём до ~2 с */
  uint32_t t0 = millis();
  while (!Serial && millis() - t0 < 2000) {
    delay(20);
  }
#endif
  delay(100);
  Serial.println();
  Serial.println(F("RuView Ultimate — ESP32-S3"));
  Serial.flush();

  pinMode(RUVIEW_RESET_PIN, INPUT_PULLUP);
  ruviewLoadConfig();

  if (!g_cfg.provisioned || g_cfg.ssid[0] == '\0') {
    startApProvisioning();
  } else {
    startSta();
  }

  printRuviewStatus();
  g_serialWasOpen = (bool)Serial;
}

void loop() {
  const uint32_t now = millis();

  bool usb = (bool)Serial;
  if (usb && !g_serialWasOpen) {
    printRuviewStatus();
  }
  g_serialWasOpen = usb;

  if (g_restartAtMs && (int32_t)(now - g_restartAtMs) >= 0) {
    ESP.restart();
  }
  pollResetButton(now);
  if (!g_temperatureAtMs || now - g_temperatureAtMs >= 5000) {
    g_chipTemperatureC = temperatureRead();
    g_temperatureAtMs = now;
  }
  if (g_disconnectLogPending && Serial) {
    g_disconnectLogPending = false;
    Serial.printf("[RuView] WiFi disconnected: reason=%u count=%u at=%u ms\n",
                  (unsigned)g_lastDisconnectReason, (unsigned)g_disconnectCount,
                  (unsigned)g_lastDisconnectMs);
  }

  if (g_apMode) {
    g_dns.processNextRequest();
  } else {
    if (g_gotIpPending) {
      g_gotIpPending = false;
      handleGotIp();
    }
    if (WiFi.status() != WL_CONNECTED && now - g_lastConnectAttemptMs >= kReconnectIntervalMs) {
      g_lastConnectAttemptMs = now;
      g_reconnectAttempts++;
      Serial.println(F("[RuView] Нет связи с роутером — переподключение"));
      WiFi.disconnect();
      WiFi.begin(g_cfg.ssid, g_cfg.pass);
    }
  }
  delay(2);
}
