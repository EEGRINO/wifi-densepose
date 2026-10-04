#include "ruview_shared.h"
#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <math.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "lwip/inet_chksum.h"
#include "lwip/prot/icmp.h"
#include "lwip/sockets.h"

/*
 * Берём только секцию L-LTF: 64 поднесущих × (imag, real) int8. Она есть в любом OFDM-кадре
 * (11g и 11n), поэтому раскладка CSI одинаковая независимо от типа пакета.
 * Порядок поднесущих в буфере: 0..31, затем -32..-1.
 * Нулевые (DC и защитные): 0, ±27..±31, -32 → полезные индексы буфера 1..26 и 38..63 (52 шт.).
 *
 * Источники CSI (линии): роутер (ответы на ping) и другие узлы RuView (маяки ESP-NOW).
 * Кадры от остальных устройств отбрасываются.
 */
#define CSI_LLTF_BYTES (RUVIEW_SC_MAX * 2)
#define CSI_QUEUE_LEN 64
#define FLAG_FROM_NODE 0x08

static const uint8_t kBeaconMagic[4] = {'R', 'U', 'V', 'B'};
static const uint8_t kBroadcast[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

struct CsiFrame {
  uint32_t seq;
  uint32_t ts_us;
  int8_t rssi;
  int8_t noise;
  uint8_t channel;
  uint8_t flags;
  uint16_t src_id;
  uint8_t src_slot;
  int8_t iq[CSI_LLTF_BYTES];
};

struct CsiSource {
  uint8_t mac[6];
  bool node;                // false — роутер, true — узел RuView
  volatile uint32_t frames; // принято кадров CSI от этого источника
};

struct CsiStats {
  volatile uint32_t total;    // все кадры CSI
  volatile uint32_t foreign;  // отброшены: не роутер и не узел RuView
  volatile uint32_t shortLen; // отброшены: нет L-LTF
  volatile uint32_t qdrop;    // отброшены: очередь переполнена
  volatile uint32_t accepted; // приняты в очередь
  uint32_t udpFail;
  uint32_t pingSent;
  uint32_t pingReplies;
  uint32_t pingFail;
  uint32_t beaconsSent;
  uint32_t beaconFail;
  volatile uint32_t espnowRx;       // все принятые ESP-NOW (любое содержимое)
  volatile uint32_t espnowTxOk;     // подтверждения отправки (колбэк)
  volatile uint32_t espnowTxErr;
  int32_t espnowRateErr;            // результат esp_wifi_config_espnow_rate
  float rateHz;               // принятых кадров/с (за последнюю секунду)
};

static QueueHandle_t g_csiQueue = nullptr;
static WiFiUDP g_udp;
static bool g_csi_on = false;
/* Слот 0 — роутер, дальше — узлы в порядке обнаружения. Пишет только задача Wi-Fi (колбэки),
 * поэтому достаточно сначала заполнить слот, а потом увеличить счётчик. */
static CsiSource g_src[RUVIEW_MAX_SOURCES];
static volatile int g_srcCount = 1;
static volatile bool g_haveApMac = false;
static uint32_t g_seq = 0;
static CsiStats g_stats = {};
static TaskHandle_t g_trafficTask = nullptr;
static TaskHandle_t g_beaconTask = nullptr;
static volatile bool g_calibrateReq = false;

static inline bool scUseful(int i) { return (i >= 1 && i <= 26) || (i >= 38 && i <= 63); }

/** Индекс буфера → позиция в частотном порядке (-32..31 → 0..63), для наглядного графика в UI. */
static inline int scToFreqPos(int i) { return i < 32 ? i + 32 : i - 32; }

static inline uint16_t macShortId(const uint8_t *mac) { return (uint16_t)((mac[4] << 8) | mac[5]); }

String ruviewNodeId() {
  uint8_t mac[6];
  esp_wifi_get_mac(WIFI_IF_STA, mac);
  char b[5];
  snprintf(b, sizeof(b), "%04X", macShortId(mac));
  return String(b);
}

static int findSource(const uint8_t *mac) {
  const int n = g_srcCount;
  if (g_haveApMac && memcmp(mac, g_src[0].mac, 6) == 0) {
    return 0;
  }
  for (int i = 1; i < n; i++) {
    if (memcmp(mac, g_src[i].mac, 6) == 0) {
      return i;
    }
  }
  return -1;
}

static void csiCallback(void *ctx, wifi_csi_info_t *info) {
  (void)ctx;
  if (!info || !info->buf) {
    return;
  }
  g_stats.total++;
  const int slot = findSource(info->mac);
  if (slot < 0) {
    g_stats.foreign++;
    return;
  }
  if (info->len < CSI_LLTF_BYTES) {
    g_stats.shortLen++;
    return;
  }
  CsiFrame f;
  f.seq = g_seq++;
  f.ts_us = info->rx_ctrl.timestamp;
  f.rssi = info->rx_ctrl.rssi;
  f.noise = info->rx_ctrl.noise_floor;
  f.channel = info->rx_ctrl.channel;
  f.flags = (uint8_t)((info->rx_ctrl.sig_mode & 0x3) | (info->first_word_invalid ? 0x04 : 0) |
                      (g_src[slot].node ? FLAG_FROM_NODE : 0));
  f.src_id = macShortId(info->mac);
  f.src_slot = (uint8_t)slot;
  memcpy(f.iq, info->buf, CSI_LLTF_BYTES);
  if (info->first_word_invalid) {
    memset(f.iq, 0, 4);
  }
  g_src[slot].frames++;
  /* seq уже израсходован: при переполнении очереди сервер увидит пропуск в нумерации. */
  if (xQueueSend(g_csiQueue, &f, 0) != pdTRUE) {
    g_stats.qdrop++;
  } else {
    g_stats.accepted++;
  }
}

/** Маяк другого узла RuView → запоминаем его MAC как источник CSI. */
static void onEspNowSent(const uint8_t *mac, esp_now_send_status_t status) {
  (void)mac;
  if (status == ESP_NOW_SEND_SUCCESS) {
    g_stats.espnowTxOk++;
  } else {
    g_stats.espnowTxErr++;
  }
}

static void onEspNowRecv(const uint8_t *mac, const uint8_t *data, int len) {
  g_stats.espnowRx++;
  if (!mac || !data || len < 4 || memcmp(data, kBeaconMagic, 4) != 0) {
    return;
  }
  if (findSource(mac) >= 0) {
    return;
  }
  const int n = g_srcCount;
  if (n >= RUVIEW_MAX_SOURCES) {
    return;
  }
  memcpy(g_src[n].mac, mac, 6);
  g_src[n].node = true;
  g_src[n].frames = 0;
  g_srcCount = n + 1;
}

void ruviewRequestCalibrate() {
  g_calibrateReq = true;
}

void ruviewCsiSetApMac(const uint8_t *mac) {
  if (!mac) {
    return;
  }
  memcpy(g_src[0].mac, mac, 6);
  g_src[0].node = false;
  g_haveApMac = true;
}

void ruviewCsiBegin() {
  if (g_csi_on) {
    return;
  }
  if (!g_csiQueue) {
    g_csiQueue = xQueueCreate(CSI_QUEUE_LEN, sizeof(CsiFrame));
  }
  wifi_csi_config_t cfg = {};
  cfg.lltf_en = true;
  cfg.htltf_en = false;
  cfg.stbc_htltf2_en = false;
  cfg.ltf_merge_en = false;
  cfg.channel_filter_en = false;
  cfg.manu_scale = false;
  esp_wifi_set_csi_config(&cfg);
  esp_wifi_set_csi_rx_cb(csiCallback, nullptr);
  esp_wifi_set_csi(true);
  g_csi_on = true;
}

void ruviewCsiStop() {
  if (!g_csi_on) {
    return;
  }
  esp_wifi_set_csi(false);
  esp_wifi_set_csi_rx_cb(nullptr, nullptr);
  g_csi_on = false;
}

/*
 * Трафик для CSI: ICMP echo к шлюзу строго по таймеру, не дожидаясь ответа (у esp_ping следующий запрос
 * уходит только после ответа — при задержке роутера 20-30 мс частота падала до 35-50 Гц).
 * Каждый ответ роутера — кадр от его MAC, т.е. кадр CSI линии «роутер → узел».
 */
static void trafficTask(void *arg) {
  (void)arg;
  int sock = -1;
  uint16_t seq = 0;
  TickType_t last = xTaskGetTickCount();
  struct __attribute__((packed)) {
    struct icmp_echo_hdr h;
    uint8_t data[8];
  } pkt;
  uint8_t rx[64];

  for (;;) {
    const uint16_t hz = g_cfg.traffic_hz > 500 ? 500 : g_cfg.traffic_hz;
    if (hz == 0 || WiFi.status() != WL_CONNECTED) {
      vTaskDelay(pdMS_TO_TICKS(500));
      last = xTaskGetTickCount();
      continue;
    }
    if (sock < 0) {
      sock = socket(AF_INET, SOCK_RAW, IPPROTO_ICMP);
      if (sock < 0) {
        g_stats.pingFail++;
        vTaskDelay(pdMS_TO_TICKS(1000));
        continue;
      }
    }
    TickType_t period = pdMS_TO_TICKS(1000 / hz);
    vTaskDelayUntil(&last, period ? period : 1);

    ICMPH_TYPE_SET(&pkt.h, ICMP_ECHO);
    ICMPH_CODE_SET(&pkt.h, 0);
    pkt.h.id = PP_HTONS(0x5256);
    pkt.h.seqno = lwip_htons(++seq);
    memset(pkt.data, 0xA5, sizeof(pkt.data));
    pkt.h.chksum = 0;
    pkt.h.chksum = inet_chksum(&pkt, sizeof(pkt));

    struct sockaddr_in to = {};
    to.sin_family = AF_INET;
    to.sin_addr.s_addr = (uint32_t)WiFi.gatewayIP();
    if (sendto(sock, &pkt, sizeof(pkt), 0, (struct sockaddr *)&to, sizeof(to)) == (int)sizeof(pkt)) {
      g_stats.pingSent++;
    } else {
      g_stats.pingFail++;
    }
    /* Ответы только считаем, чтобы буфер сокета не переполнялся. */
    while (recv(sock, rx, sizeof(rx), MSG_DONTWAIT) > 0) {
      g_stats.pingReplies++;
    }
  }
}

void ruviewTrafficRestart() {
  /* Задача сама читает g_cfg.traffic_hz и адрес шлюза — достаточно запустить её один раз. */
  if (!g_trafficTask) {
    xTaskCreatePinnedToCore(trafficTask, "ruv_traffic", 3072, nullptr, 3, &g_trafficTask, 1);
  }
  Serial.printf("[RuView] Трафик для CSI: ICMP → %s @ %u Гц\n", WiFi.gatewayIP().toString().c_str(),
                g_cfg.traffic_hz);
}

static void beaconTask(void *arg) {
  (void)arg;
  struct __attribute__((packed)) {
    uint8_t magic[4];
    uint32_t seq;
  } msg;
  memcpy(msg.magic, kBeaconMagic, 4);
  msg.seq = 0;
  TickType_t last = xTaskGetTickCount();
  for (;;) {
    const uint16_t hz = g_cfg.beacon_hz > 500 ? 500 : g_cfg.beacon_hz;
    if (hz == 0) {
      vTaskDelay(pdMS_TO_TICKS(500));
      last = xTaskGetTickCount();
      continue;
    }
    TickType_t period = pdMS_TO_TICKS(1000 / hz);
    if (period == 0) {
      period = 1;
    }
    vTaskDelayUntil(&last, period);
    if (WiFi.status() != WL_CONNECTED) {
      continue; /* без роутера канал «гуляет» при поиске сети — маяки бесполезны */
    }
    msg.seq++;
    if (esp_now_send(kBroadcast, (const uint8_t *)&msg, sizeof(msg)) == ESP_OK) {
      g_stats.beaconsSent++;
    } else {
      g_stats.beaconFail++;
    }
  }
}

void ruviewBeaconBegin() {
  if (g_beaconTask) {
    return;
  }
  if (esp_now_init() != ESP_OK) {
    Serial.println(F("[RuView] ESP-NOW init failed"));
    return;
  }
  /* По умолчанию ESP-NOW идёт на 1 Мбит/с (802.11b, DSSS) — в таких кадрах нет L-LTF и CSI не измерить.
   * 6 Мбит/с 11g — OFDM-кадр с L-LTF. HT MCS0 не годится: проверено на железе — узлы, подключённые
   * к роутеру, такие кадры ESP-NOW не принимают вовсе. */
  g_stats.espnowRateErr = esp_wifi_config_espnow_rate(WIFI_IF_STA, WIFI_PHY_RATE_6M);
  esp_now_register_recv_cb(onEspNowRecv);
  esp_now_register_send_cb(onEspNowSent);

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, kBroadcast, 6);
  peer.channel = 0; /* текущий канал — канал роутера */
  peer.ifidx = WIFI_IF_STA;
  peer.encrypt = false;
  if (!esp_now_is_peer_exist(kBroadcast)) {
    esp_now_add_peer(&peer);
  }
  xTaskCreatePinnedToCore(beaconTask, "ruv_beacon", 3072, nullptr, 3, &g_beaconTask, 1);
  Serial.printf("[RuView] Узел %s: маяки ESP-NOW @ %u Гц\n", ruviewNodeId().c_str(), g_cfg.beacon_hz);
}

void ruviewCsiFillStatus(JsonDocument &doc) {
  doc["node_id"] = ruviewNodeId();
  JsonObject c = doc["csi"].to<JsonObject>();
  c["rate_hz"] = g_stats.rateHz;
  c["total"] = g_stats.total;
  c["accepted"] = g_stats.accepted;
  c["foreign"] = g_stats.foreign;
  c["short"] = g_stats.shortLen;
  c["queue_drop"] = g_stats.qdrop;
  c["udp_fail"] = g_stats.udpFail;
  c["traffic_hz"] = g_cfg.traffic_hz;
  c["ping_sent"] = g_stats.pingSent;
  c["ping_replies"] = g_stats.pingReplies;
  c["ping_fail"] = g_stats.pingFail;
  c["beacon_hz"] = g_cfg.beacon_hz;
  c["beacons_sent"] = g_stats.beaconsSent;
  c["beacon_fail"] = g_stats.beaconFail;
  c["espnow_rx"] = g_stats.espnowRx;
  c["espnow_tx_ok"] = g_stats.espnowTxOk;
  c["espnow_tx_err"] = g_stats.espnowTxErr;
  c["espnow_rate_err"] = g_stats.espnowRateErr;
  JsonArray arr = c["sources"].to<JsonArray>();
  const int n = g_srcCount;
  for (int i = 0; i < n; i++) {
    if (i == 0 && !g_haveApMac) {
      continue;
    }
    JsonObject o = arr.add<JsonObject>();
    char b[18];
    const uint8_t *m = g_src[i].mac;
    snprintf(b, sizeof(b), "%02X:%02X:%02X:%02X:%02X:%02X", m[0], m[1], m[2], m[3], m[4], m[5]);
    o["mac"] = b;
    o["kind"] = g_src[i].node ? "node" : "router";
    o["frames"] = g_src[i].frames;
  }
}

/** RuView wifi-densepose-sensing-server: magic 0xC5110001, int8 I/Q pairs (см. main.rs parse_esp32_frame). */
static void sendUdpAdr018(const IPAddress &ip, const CsiFrame &f) {
  if (g_cfg.ruview_adr_port == 0) {
    return;
  }
  const uint16_t nPairs = RUVIEW_SC_MAX;
  uint8_t pkt[20 + CSI_LLTF_BYTES];

  uint32_t magic = 0xC5110001u;
  memcpy(pkt, &magic, 4);
  pkt[4] = 1;
  pkt[5] = 1;
  pkt[6] = (uint8_t)nPairs;
  pkt[7] = 0;
  uint16_t freq_mhz = 2437;
  if (f.channel >= 1 && f.channel <= 13) {
    freq_mhz = (uint16_t)(2407 + f.channel * 5);
  }
  memcpy(pkt + 8, &freq_mhz, 2);
  memcpy(pkt + 10, &f.seq, 4);
  pkt[14] = (uint8_t)f.rssi;
  pkt[15] = (uint8_t)f.noise;
  memset(pkt + 16, 0, 4);
  memcpy(pkt + 20, f.iq, CSI_LLTF_BYTES);

  if (g_udp.beginPacket(ip, g_cfg.ruview_adr_port)) {
    g_udp.write(pkt, sizeof(pkt));
    g_udp.endPacket();
  }
}

/*
 * Пакет RUV2 (little-endian), 148 байт:
 *   0  4  "RUV2"
 *   4  4  seq       uint32 — номер принятого кадра CSI (общий для всех источников); пропуски = потери
 *   8  4  ts_us     uint32 — время приёма кадра радиомодулем, мкс (переполнение ~71 мин)
 *  12  1  rssi      int8, дБм
 *  13  1  noise     int8, дБм (шумовой порог)
 *  14  1  channel   uint8
 *  15  1  flags     биты 0-1: sig_mode (0 — 11b/g, 1 — 11n); бит 2: первые 4 байта обнулены;
 *                   бит 3: источник — узел RuView (иначе роутер)
 *  16  2  n_sc      uint16 = 64
 *  18  2  src_id    uint16 — последние 2 байта MAC передатчика (линия «src → этот узел»)
 *  20  …  n_sc × (imag int8, real int8), порядок поднесущих 0..31, -32..-1
 */
static void sendUdpRuv2(const IPAddress &ip, const CsiFrame &f) {
  uint8_t pkt[20 + CSI_LLTF_BYTES];
  memcpy(pkt, RUVIEW_UDP_MAGIC, 4);
  memcpy(pkt + 4, &f.seq, 4);
  memcpy(pkt + 8, &f.ts_us, 4);
  pkt[12] = (uint8_t)f.rssi;
  pkt[13] = (uint8_t)f.noise;
  pkt[14] = f.channel;
  pkt[15] = f.flags;
  uint16_t n = RUVIEW_SC_MAX;
  memcpy(pkt + 16, &n, 2);
  memcpy(pkt + 18, &f.src_id, 2);
  memcpy(pkt + 20, f.iq, CSI_LLTF_BYTES);

  if (!g_udp.beginPacket(ip, g_cfg.server_port) || g_udp.write(pkt, sizeof(pkt)) != sizeof(pkt) ||
      !g_udp.endPacket()) {
    g_stats.udpFail++;
  }
}

void ruviewStreamTask(void *arg) {
  (void)arg;
  g_udp.begin(0);

  static float amp[RUVIEW_SC_MAX];
  static float prevAmp[RUVIEW_SC_MAX];
  static float baseline[RUVIEW_SC_MAX];
  static float disp[RUVIEW_SC_MAX];
  bool havePrev = false;
  bool haveBaseline = false;
  bool haveSnap = false;
  bool freshSinceWs = false;
  float motion = 0.f;
  int8_t lastRssi = 0;
  uint32_t lastTs = 0;

  uint32_t lastWs = 0;
  uint32_t lastSec = millis();
  uint32_t acceptedAtSec = 0;

  IPAddress dst;
  char dstStr[sizeof(g_cfg.server_ip)] = "";
  bool dstOk = false;

  CsiFrame f;
  for (;;) {
    const bool got = g_csiQueue && xQueueReceive(g_csiQueue, &f, pdMS_TO_TICKS(20)) == pdTRUE;
    const uint32_t now = millis();

    if (got) {
      /* server_ip меняется из веб-панели — перепарсиваем только при изменении строки. */
      if (strncmp(dstStr, g_cfg.server_ip, sizeof(dstStr)) != 0) {
        strlcpy(dstStr, g_cfg.server_ip, sizeof(dstStr));
        dstOk = dstStr[0] != '\0' && dst.fromString(dstStr);
      }
      if (dstOk && g_cfg.server_port != 0) {
        sendUdpRuv2(dst, f);
        sendUdpAdr018(dst, f);
      }
    }

    /* Встроенный UI показывает одну линию — от роутера: соседние кадры разных линий сравнивать нельзя. */
    if (got && f.src_slot == 0) {
      for (int i = 0; i < RUVIEW_SC_MAX; i++) {
        float im = (float)f.iq[2 * i];
        float re = (float)f.iq[2 * i + 1];
        amp[i] = scUseful(i) ? sqrtf(re * re + im * im) : 0.f;
      }
      if (g_calibrateReq) {
        g_calibrateReq = false;
        memcpy(baseline, amp, sizeof(baseline));
        haveBaseline = true;
      }
      /* Грубая метрика для встроенного UI: средний модуль изменения амплитуды между кадрами. */
      if (havePrev) {
        float s = 0.f;
        for (int i = 0; i < RUVIEW_SC_MAX; i++) {
          s += fabsf(amp[i] - prevAmp[i]);
        }
        motion = s / 52.f;
      }
      memcpy(prevAmp, amp, sizeof(prevAmp));
      havePrev = true;

      for (int i = 0; i < RUVIEW_SC_MAX; i++) {
        float v = amp[i];
        if (haveBaseline) {
          v -= baseline[i];
          if (v < 0.f) {
            v = 0.f;
          }
        }
        disp[scToFreqPos(i)] = v;
      }
      lastRssi = f.rssi;
      lastTs = f.ts_us;
      haveSnap = true;
      freshSinceWs = true;
    }

    if (now - lastSec >= 1000) {
      const uint32_t acc = g_stats.accepted;
      g_stats.rateHz = (float)(acc - acceptedAtSec) * 1000.f / (float)(now - lastSec);
      acceptedAtSec = acc;
      lastSec = now;
      g_ws.cleanupClients();
    }

    /* Браузеру хватает ~20 Гц; полный поток идёт по UDP. */
    if (haveSnap && g_ws.count() > 0 && now - lastWs >= 50) {
      lastWs = now;
      JsonDocument doc;
      doc["ts"] = lastTs;
      doc["rssi"] = lastRssi;
      doc["motion"] = motion;
      doc["n"] = RUVIEW_SC_MAX;
      doc["fresh"] = freshSinceWs;
      JsonArray arr = doc["a"].to<JsonArray>();
      for (int i = 0; i < RUVIEW_SC_MAX; i++) {
        arr.add(disp[i]);
      }
      String out;
      serializeJson(doc, out);
      g_ws.textAll(out);
      freshSinceWs = false;
    }
  }
}
