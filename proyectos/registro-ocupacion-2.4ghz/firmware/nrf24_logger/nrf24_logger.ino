// 2.4 GHz occupancy logger with a single nRF24L01+ (CE=4, CSN=5).
// Every sweep visits the 126 channels (2400..2525 MHz) PASSES times and counts
// how often the RPD carrier detector (> about -64 dBm) fires on each channel.
// Output, one CSV line per sweep (timestamps are added by the PC logger):
//   S,<sweep_index>,<sweep_ms>,<count_ch0>,...,<count_ch125>
// Lines starting with '#' are comments.
#include <SPI.h>   // SPI bus driver (ships with the ESP32 core)
#include "RF24.h"  // TMRh20 RF24 library for the nRF24L01+

constexpr uint8_t PIN_CE = 4;
constexpr uint8_t PIN_CSN = 5;
constexpr uint8_t PIN_CSN_UNUSED = 17;  // keep a second radio deselected if present
constexpr uint8_t NUM_CHANNELS = 126;
constexpr uint8_t PASSES = 30;
constexpr uint16_t SETTLE_US = 130;  // RX settling + RPD sampling time

RF24 radio(PIN_CE, PIN_CSN);
uint8_t hits[NUM_CHANNELS];
uint32_t sweepIndex = 0;

void setup() {
  Serial.begin(115200);
  delay(500);
  pinMode(PIN_CSN_UNUSED, OUTPUT);
  digitalWrite(PIN_CSN_UNUSED, HIGH);

  SPI.begin();  // SCK=18, MISO=19, MOSI=23
  if (!radio.begin(&SPI) || !radio.isChipConnected()) {
    Serial.println("# ERROR radio not found on CE=4/CSN=5");
    while (true) delay(1000);
  }
  radio.setAutoAck(false);
  radio.setDataRate(RF24_1MBPS);
  radio.startListening();
  radio.stopListening();
  Serial.printf("# nrf24_logger channels=%u passes=%u settle_us=%u\n", NUM_CHANNELS, PASSES, SETTLE_US);
}

void loop() {
  memset(hits, 0, sizeof(hits));
  uint32_t start = millis();
  for (uint8_t pass = 0; pass < PASSES; pass++) {
    for (uint8_t ch = 0; ch < NUM_CHANNELS; ch++) {
      radio.setChannel(ch);
      radio.startListening();
      delayMicroseconds(SETTLE_US);
      if (radio.testRPD()) hits[ch]++;
      radio.stopListening();
    }
  }
  uint32_t elapsed = millis() - start;

  Serial.printf("S,%lu,%lu", (unsigned long)sweepIndex++, (unsigned long)elapsed);
  for (uint8_t ch = 0; ch < NUM_CHANNELS; ch++) Serial.printf(",%u", hits[ch]);
  Serial.println();
}
