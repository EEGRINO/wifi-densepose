#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>
#include <AsyncTCP.h>
#include <ESPAsyncWebServer.h>
#include <Preferences.h>

#define RUVIEW_AP_SSID "RuView_Setup"
#define RUVIEW_UDP_MAGIC "RUV2"
#define RUVIEW_UDP_PORT_DEFAULT 5555
#define RUVIEW_SC_MAX 64
#define RUVIEW_HOSTNAME "ruview-csi"
/** Частота собственного ping-трафика к роутеру (каждый ответ роутера = кадр CSI). 0 — выключено. */
#define RUVIEW_TRAFFIC_HZ_DEFAULT 100
/** Частота маяков ESP-NOW (11g 6 Мбит/с): по ним другие узлы измеряют CSI линии «этот узел → они». 0 — выключено. */
#define RUVIEW_BEACON_HZ_DEFAULT 100
/** Сколько источников CSI помнит узел: роутер + узлы RuView. */
#define RUVIEW_MAX_SOURCES 16
/** Кнопка BOOT: держать 5 с во время работы — сброс Wi-Fi и режим RuView_Setup (не при включении: это режим прошивки). */
#define RUVIEW_RESET_PIN 0

struct RuViewConfig {
  char ssid[33];
  char pass[65];
  char server_ip[46];
  uint16_t server_port;
  /** Если не 0 — дублировать CSI на этот UDP-порт в формате RuView ADR-018 (0xC5110001, int8 I/Q), тот же server_ip. Напр. 5005 для wifi-densepose-sensing-server. */
  uint16_t ruview_adr_port;
  uint16_t traffic_hz;
  uint16_t beacon_hz;
  /** С этими SSID/паролем уже было успешное подключение: при обрыве не стираем настройки, а переподключаемся. */
  bool connected_once;
  bool provisioned;
};

extern RuViewConfig g_cfg;
extern AsyncWebServer g_server;
extern AsyncWebSocket g_ws;
extern Preferences g_prefs;

void ruviewLoadConfig();
void ruviewSaveConfig();
void ruviewClearConfig();
void ruviewRequestRestart(uint32_t delayMs);
void ruviewRequestCalibrate();
void ruviewHttpBegin(bool apMode);
void ruviewCsiBegin();
void ruviewCsiStop();
/** MAC роутера (BSSID): CSI принимается от него и от узлов RuView (по маякам ESP-NOW). */
void ruviewCsiSetApMac(const uint8_t *mac);
/** (Пере)запуск ping к шлюзу с частотой g_cfg.traffic_hz. */
void ruviewTrafficRestart();
/** ESP-NOW: маяки этого узла и приём маяков других узлов (после подключения к роутеру — на его канале). */
void ruviewBeaconBegin();
/** Короткий ID узла — последние 2 байта MAC, как его видят другие узлы ("75BC"). */
String ruviewNodeId();
void ruviewCsiFillStatus(JsonDocument &doc);
void ruviewStreamTask(void *arg);
