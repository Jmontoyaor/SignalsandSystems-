// Raw SPI probe for two nRF24L01+ modules on a shared VSPI bus.
// Reads STATUS/CONFIG/SETUP_AW and does an RF_CH write/readback per module.
// Expected values for a healthy module after power-up:
//   STATUS=0x0E  CONFIG=0x08  SETUP_AW=0x03  RF_CH readback=0x2A
#include <SPI.h>
#include <driver/gpio.h>

struct Probe {
  const char *name;
  uint8_t ce, csn;
};
Probe probes[] = {{"A", 4, 5}, {"B", 16, 17}};

constexpr uint8_t PIN_MISO = 19;

uint8_t readReg(uint8_t csn, uint8_t reg, uint8_t *status) {
  SPI.beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE0));
  digitalWrite(csn, LOW);
  *status = SPI.transfer(reg & 0x1F);
  uint8_t value = SPI.transfer(0xFF);
  digitalWrite(csn, HIGH);
  SPI.endTransaction();
  return value;
}

void writeReg(uint8_t csn, uint8_t reg, uint8_t value) {
  SPI.beginTransaction(SPISettings(1000000, MSBFIRST, SPI_MODE0));
  digitalWrite(csn, LOW);
  SPI.transfer(0x20 | (reg & 0x1F));
  SPI.transfer(value);
  digitalWrite(csn, HIGH);
  SPI.endTransaction();
}

const char *diagnose(uint8_t st, uint8_t cfg, uint8_t aw, uint8_t ch) {
  if (ch == 0x2A && aw == 0x03) return "OK: module responds";
  if (st == 0x00 && cfg == 0x00) return "FAIL: all 0x00 -> MISO stuck low / no power / CSN not reaching module";
  if (st == 0xFF && cfg == 0xFF) return "FAIL: all 0xFF -> MISO floating / module not powered / GND missing";
  return "FAIL: inconsistent data -> loose wire, noisy power (add 10uF cap) or damaged module";
}

void probeAll(bool pullup) {
  if (pullup) {
    gpio_pulldown_dis(GPIO_NUM_19);
    gpio_pullup_en(GPIO_NUM_19);
  } else {
    gpio_pullup_dis(GPIO_NUM_19);
    gpio_pulldown_en(GPIO_NUM_19);
  }
  Serial.printf("-- MISO %s --\n", pullup ? "pull-up" : "pull-down");
  for (auto &p : probes) {
    uint8_t st, st2, st3;
    uint8_t cfg = readReg(p.csn, 0x00, &st);
    uint8_t aw = readReg(p.csn, 0x03, &st2);
    writeReg(p.csn, 0x05, 0x2A);
    uint8_t ch = readReg(p.csn, 0x05, &st3);
    writeReg(p.csn, 0x05, 0x02);  // restore default channel
    Serial.printf("[%s] STATUS=0x%02X CONFIG=0x%02X SETUP_AW=0x%02X RF_CH=0x%02X  %s\n",
                  p.name, st, cfg, aw, ch, diagnose(st, cfg, aw, ch));
  }
}

void setup() {
  Serial.begin(115200);
  delay(500);
  for (auto &p : probes) {
    pinMode(p.csn, OUTPUT);
    digitalWrite(p.csn, HIGH);
    pinMode(p.ce, OUTPUT);
    digitalWrite(p.ce, LOW);
  }
  SPI.begin();  // SCK=18, MISO=19, MOSI=23
  delay(100);
}

void loop() {
  Serial.println("\n===== nRF24L01+ SPI diagnostic =====");
  probeAll(false);
  probeAll(true);
  delay(2000);
}
