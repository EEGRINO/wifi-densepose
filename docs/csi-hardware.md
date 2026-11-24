# Выбор платы для Wi‑Fi CSI при 20 МГц

**ESP32‑C5‑WROOM‑1‑N4** подходит как кандидат для следующего стенда. Он добавляет 5 ГГц и приём HE20 при ширине канала 20 МГц. Рабочая прошивка этого репозитория рассчитана на ESP32‑S3. Для C5 нужен отдельный профиль сборки и адаптация приёма CSI. Сравнительные измерения C5 на нашем стенде ещё предстоят. [Спецификация C5](https://documentation.espressif.com/esp32-c5-wroom-1_wroom-1u_datasheet_en.html), [форматы CSI](https://docs.espressif.com/projects/esp-idf/en/latest/esp32c5/api-guides/wifi-driver/wifi-vendor-features.html#wi-fi-channel-state-information).

## Микроконтроллеры для своей прошивки

| Модуль или чип | Wi‑Fi при 20 МГц | Память указанной версии | Роль на стенде |
| --- | --- | --- | --- |
| **ESP32‑S3‑WROOM‑1‑N8R2** | Wi‑Fi 4, 2,4 ГГц, 1T1R | 512 КБ SRAM, 8 МБ флеш, 2 МБ PSRAM | Рабочая прошивка проекта и контрольная серия. [Espressif](https://documentation.espressif.com/esp32-s3-wroom-1_wroom-1u_datasheet_en.html) |
| **ESP32‑C5‑WROOM‑1‑N4** | Wi‑Fi 6 HE20, 2,4 и 5 ГГц, 1T1R | 384 КБ основной SRAM, 16 КБ LP SRAM, 4 МБ флеш, без PSRAM | Сравнение диапазонов и HE20 на компактном узле. [Espressif](https://documentation.espressif.com/esp32-c5-wroom-1_wroom-1u_datasheet_en.html) |
| **ESP32‑C6‑WROOM‑1‑N8** | Wi‑Fi 6 HE20, 2,4 ГГц, 1T1R | 512 КБ основной SRAM, 16 КБ LP SRAM, 8 МБ флеш | Контрольный вариант, если нужен HE20 только на 2,4 ГГц. [Espressif](https://documentation.espressif.com/esp32-c6-wroom-1_wroom-1u_datasheet_en.html) |
| **Realtek RTL8720EAF‑VA2‑CG** | Wi‑Fi 6, 2,4 ГГц | 768 КБ SRAM, 4 МБ флеш | Тест альтернативного SDK с CSI на микроконтроллере. [Спецификации Realtek](https://ameba-aiot.github.io/ameba-iot-docs/linux/en/latest/rst_rtos/0_ameba_product/0_ameba_product_center_index.html) |
| **Realtek RTL8726EAM‑VA4‑CG** | Wi‑Fi 6, 2,4 ГГц | 512 КБ SRAM, 16 МБ PSRAM, внешняя флеш на плате | Вариант для фильтрации и спектрального анализа на самом узле. Производитель указывает отдельный DSP 500 МГц. [Спецификации Realtek](https://ameba-aiot.github.io/ameba-iot-docs/linux/en/latest/rst_rtos/0_ameba_product/0_ameba_product_center_index.html) |
| **Realtek RTL8730EAH‑VH6‑CG** | Wi‑Fi 6, 2,4 и 5 ГГц | 256 КБ SRAM, 64 МБ DDR2, 32 МБ флеш | Вариант для более тяжёлой обработки на плате. Поиск платы имеет смысл вести по точной маркировке, поскольку объём памяти зависит от исполнения чипа. [Спецификация RTL8730E](https://aiot.realmcu.com/cn/_static/datasheet/ameba_datasheet_rtl8730e_v5.5.pdf) |

1T1R означает один передающий и один приёмный радиотракт. SRAM и внешняя оперативная память определяют объём буферов и обработки на узле. Флеш отводится под прошивку и постоянные данные. Перед заказом проверьте маркировку модуля и наличие USB для прошивки на самой плате. У C5 версия **WROOM‑1U** имеет разъём для внешней антенны, версия **WROOM‑1** использует печатную антенну. У отладочной платы **ESP32-C5-DevKitC-1-N8R8** 8 МБ флеш и 8 МБ PSRAM. [Модули C5](https://documentation.espressif.com/esp32-c5-wroom-1_wroom-1u_datasheet_en.html), [официальная отладочная плата](https://docs.espressif.com/projects/esp-dev-kits/en/latest/esp32c5/esp32-c5-devkitc-1/).

Realtek публикует собственный [SDK Ameba RTOS](https://github.com/Ameba-AIoT/ameba-rtos) и описывает режимы, буфер и разрядность I/Q в [руководстве по CSI](https://ameba-aiot.github.io/ameba-iot-docs/freertos/en/latest/rst_rtos/2_wifi_csi/1_wifi_csi_toprst.html). Для включения платы Ameba в этот проект потребуется свой отправитель UDP и проверка раскладки, масштабирования и частоты кадров.

## Варианты для измерений на Linux

| Вариант | Что даёт при 20 МГц | Требования |
| --- | --- | --- |
| **Intel AX210 + PicoScenes** | До двух пространственных потоков. Можно измерять CSI на нескольких приёмных трактах на одном адаптере. | Компьютер с Linux, совместимый слот M.2, подходящие антенны и поддерживаемая конфигурация PicoScenes. [Intel](https://cdrdv2-public.intel.com/626913/626913_Intel_Wi-Fi_6E_AX210_Typhoon_Peak_2_Product_Brief_Rev1p4.pdf), [PicoScenes](https://ps.zpj.io/manual/hardware.html) |
| **Raspberry Pi 4B + Nexmon CSI** | Linux на самом узле. Можно собирать CSI и обрабатывать его на плате через Python. | Поддерживаемая связка чипа, прошивки Wi‑Fi и Raspberry Pi OS. Nexmon указывает BCM43455c0 с прошивкой 7.45.189. [Nexmon CSI](https://github.com/seemoo-lab/nexmon_csi) |
| **Qualcomm Atheros QCA9300 + PicoScenes** | Исследовательский вариант с максимум тремя трактами в 802.11n при подходящем передатчике. | Linux и кадры HT sounding. У PicoScenes есть таблица совместимости передатчиков и приёмников. [PicoScenes](https://ps.zpj.io/manual/scenarios.html#interoperability-among-sdr-and-cots-nics) |

## Что изменится с C5

- **Дополнительный диапазон.** Длина волны составляет около 12,5 см на 2,4 ГГц и около 6 см на 5 ГГц. Одно и то же смещение отражающего пути сильнее меняет фазу на 5 ГГц. Практический результат зависит от расстановки, многолучёвости и повторяемости измерений. WROOM‑1 использует одну антенну для обоих диапазонов и переключается между ними. [Espressif FAQ, § 4.11.146](https://docs.espressif.com/projects/esp-faq/en/latest/esp-faq-en-master.pdf)
- **Больше частотных отсчётов в HE20.** В CSI C5 для HE20 SU документация указывает 245 позиций, включая 242 используемые поднесущие. Для HT20 указано 56 используемых. Текущий приёмник RUV2 обрабатывает 64 позиции L‑LTF с 52 полезными поднесущими. Чтобы получить HE‑LTF, нужен передатчик, отправляющий подходящие HE20‑кадры, и соответствующая настройка драйвера C5. [ESP‑IDF](https://docs.espressif.com/projects/esp-idf/en/latest/esp32c5/api-guides/wifi-driver/wifi-vendor-features.html#wi-fi-channel-state-information), [текущий разбор RUV2](../laptop_server/csi_packet.py)
- **Разнесённые приёмники.** Два C5 с разных сторон комнаты дают два ракурса при одном передатчике. Для объединения измерений на ноутбуке нужны отметки времени и калибровка каждой линии. Для совместной обработки абсолютной фазы дополнительно требуется фазовая синхронизация. Espressif описывает экспериментальную схему с общей тактовой частотой. [ESP‑CSI и схема с общим кварцем](https://docs.espressif.com/projects/esp-techpedia/en/latest/esp-friends/solution-introduction/esp-csi/esp-csi-solution.html)

Плотность поднесущих определяет, насколько подробно измеряется частотный отклик внутри тех же 20 МГц. Оценку положения, дыхательного ритма и суставов нужно проверять отдельными замерами.

## План сравнения S3 и C5

1. **Проверить порт на ESP‑IDF.** Начать с примера [esp-csi](https://github.com/espressif/esp-csi). На стенде проверить тип кадра, PHY, полосу, порядок I/Q и длину CSI. Для сопоставимого L‑LTF привести данные к формату RUV2. Для HE‑LTF предусмотреть отдельный формат с указанием PHY, полосы и числа отсчётов.
2. **Поменять один параметр за серию.** Сначала S3 и C5 на 2,4 ГГц с одинаковым передатчиком и аналогичными кадрами L‑LTF. Затем C5 с совместимым источником HE20 отдельно на 2,4 и 5 ГГц. Фиксировать позиции, высоты, ориентацию антенн и ширину 20 МГц. Перед каждой серией записать фон.
3. **Сравнить качество потока и результат.** Записать кадры в секунду по каждой линии, пропуски последовательности, RSSI и разброс CSI в пустой комнате. Для движения и дыхания оценить задержку, ложные срабатывания на пустой комнате и погрешность частоты относительно ручного подсчёта. Для карты сравнить ошибку на контрольных точках, которые не участвовали в обучении.
