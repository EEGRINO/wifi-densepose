---
license: mit
tags:
  - wifi-sensing
  - vital-signs
  - presence-detection
  - edge-ai
  - esp32
  - self-supervised
  - cognitum
  - through-wall
  - privacy-preserving
  - spiking-neural-network
  - ruvector
language:
  - en
library_name: onnxruntime
pipeline_tag: other
---

# RuView — WiFi Sensing Models

**Turn WiFi signals into spatial intelligence.** Detect people, measure breathing and heart rate, track movement, and monitor rooms — through walls, in the dark, with no cameras. Just radio physics.

## What This Does

WiFi signals bounce off people. When someone breathes, their chest moves the air, which subtly changes the WiFi signal. When they walk, the changes are bigger. This model learned to read those changes from a $9 ESP32 chip.

| What it senses | How well | Without |
|----------------|----------|---------|
| **Is someone there?** | 100% accuracy | No camera needed |
| **Are they moving?** | Detects typing vs walking vs standing | No wearable needed |
| **Breathing rate** | 6-30 BPM, contactless | No chest strap |
| **Heart rate** | 40-120 BPM, through clothes | No smartwatch |
| **How many people?** | 1-4, via subcarrier graph analysis | No headcount camera |
| **Through walls** | Works through drywall, wood, fabric | No line of sight |
| **Sleep quality** | Deep/Light/REM/Awake classification | No mattress sensor |
| **Fall detection** | <2 second alert | No pendant |

## Benchmarks

Validated on real hardware (Apple M4 Pro + 2x ESP32-S3):

| Metric | Result | Context |
|--------|--------|---------|
| **Presence accuracy** | **100%** | Never misses, never false alarms |
| **Inference speed** | **0.008 ms** | 125,000x faster than real-time |
| **Throughput** | **164,183 emb/sec** | One laptop handles 1,600+ sensors |
| **Contrastive learning** | **51.6% improvement** | Trained on 8 hours of overnight data |
| **Model size** | **8 KB** (4-bit quantized) | Fits in ESP32 SRAM |
| **Training time** | **12 minutes** | On Mac Mini M4 Pro, no GPU needed |
| **Camera required** | **No** | Trained from 10 sensor signals |

## Models in This Repo

| File | Size | Use |
|------|------|-----|
| `model.safetensors` | 48 KB | Full contrastive encoder (128-dim embeddings) |
| `model-q4.bin` | 8 KB | **Recommended** — 4-bit quantized, 8x compression |
| `model-q2.bin` | 4 KB | Ultra-compact for ESP32 edge inference |
| `model-q8.bin` | 16 KB | High quality 8-bit |
| `presence-head.json` | 2.6 KB | Presence detection head (100% accuracy) |
| `node-1.json` | 21 KB | LoRA adapter for room/node 1 |
| `node-2.json` | 21 KB | LoRA adapter for room/node 2 |
| `config.json` | 586 B | Model configuration |
| `training-metrics.json` | 3.1 KB | Loss curves and training history |

## Quick Start

```bash
# Download models
pip install huggingface_hub
huggingface-cli download ruv/ruview --local-dir models/

# Use with RuView sensing pipeline
git clone https://github.com/ruvnet/RuView.git
cd RuView

# Flash an ESP32-S3 ($9 on Amazon/AliExpress)
python -m esptool --chip esp32s3 --port COM9 --baud 460800 \
  write_flash 0x0 bootloader.bin 0x8000 partition-table.bin \
  0xf000 ota_data_initial.bin 0x20000 esp32-csi-node.bin

# Provision WiFi
python firmware/esp32-csi-node/provision.py --port COM9 \
  --ssid "YourWiFi" --password "secret" --target-ip YOUR_IP

# See what WiFi reveals about your room
node scripts/deep-scan.js --bind YOUR_IP --duration 10
```

## Architecture

```
WiFi signals → ESP32-S3 ($9) → 8-dim features @ 1 Hz → Encoder → 128-dim embedding
                                                                    ↓
                                         ┌──────────────────────────┼──────────────────┐
                                         ↓                          ↓                  ↓
                                    Presence head            Activity head         Vitals head
                                    (100% accuracy)          (still/walk/talk)     (BR, HR)
```

The encoder converts 8 WiFi Channel State Information (CSI) features into a 128-dimensional embedding:

| Dim | Feature | What it captures |
|-----|---------|-----------------|
| 0 | Presence | How much the WiFi signal is disturbed |
| 1 | Motion | Rate of signal change (walking > typing > still) |
| 2 | Breathing | Chest movement modulates subcarrier phase at 6-30 BPM |
| 3 | Heart rate | Blood pulse creates micro-Doppler at 40-120 BPM |
| 4 | Phase variance | Signal quality — higher = more movement |
| 5 | Person count | Independent motion clusters via min-cut graph |
| 6 | Fall detected | Sudden phase acceleration followed by stillness |
| 7 | RSSI | Signal strength — indicates distance from sensor |

## Training Details

**No camera was used.** Trained using self-supervised contrastive learning:

- **Data**: 60,630 samples from 2 ESP32-S3 nodes over 8 hours
- **Method**: Triplet loss + InfoNCE (nearby frames = similar, distant = different)
- **Augmentation**: 10x via temporal interpolation, noise, cross-node blending
- **Supervision**: PIR sensor, BME280, RSSI triangulation, subcarrier asymmetry
- **Quantization**: TurboQuant 2/4/8-bit with <0.5% quality loss
- **Adaptation**: LoRA rank-4 per room, EWC to prevent forgetting

## 17 Sensing Applications

Built on these embeddings ([RuView](https://github.com/ruvnet/RuView)):

**Core:** Presence, person counting, RF scanning, SNN learning, CNN fingerprinting

**Health:** Sleep monitoring, apnea screening, stress detection, gait analysis

**Environment:** Room fingerprinting, material detection, device fingerprinting

**Multi-frequency:** RF tomography, passive radar, material classification, through-wall motion

## Hardware

| Component | Cost | Purpose |
|-----------|------|---------|
| ESP32-S3 (8MB) | ~$9 | WiFi CSI sensing |
| [Cognitum Seed](https://cognitum.one) (optional) | $131 | Persistent storage, kNN, witness chain, AI proxy |

## Limitations

- Room-specific (use LoRA adapters for new rooms)
- Camera-free pose: 2.5% PCK@20 (camera labels improve significantly)
- Health features are for screening only, not medical diagnosis
- Breathing/HR less accurate during active movement

## Citation

```bibtex
@software{ruview2026,
  title={RuView: WiFi Sensing with Self-Supervised Contrastive Learning},
  author={rUv},
  year={2026},
  url={https://github.com/ruvnet/RuView},
  note={Models: https://huggingface.co/ruv/ruview}
}
```

## Links

- **GitHub**: https://github.com/ruvnet/RuView
- **Cognitum Seed**: https://cognitum.one
- **RuVector**: https://github.com/ruvnet/ruvector
- **License**: MIT
