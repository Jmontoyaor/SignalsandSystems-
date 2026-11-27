// 2.4 GHz spectrum analyzer with a single nRF24L01+ (position A: CE=4, CSN=5).
// Sweeps the 126 channels (2400..2525 MHz) using the RPD carrier detector
// (signal stronger than about -64 dBm) and prints one waterfall row per sweep.
// Each character is one channel: ' ' = quiet ... '@' = busy on every pass.
#include <SPI.h>   // SPI bus driver (ships with the ESP32 core)
#include "RF24.h"  // TMRh20 RF24 library for the nRF24L01+

constexpr uint8_t PIN_CE = 4;
constexpr uint8_t PIN_CSN = 5;
constexpr uint8_t PIN_CSN_UNUSED = 17;  // keep radio B deselected if present
constexpr uint8_t NUM_CHANNELS = 126;
constexpr uint8_t PASSES = 30;           // samples per channel per sweep
constexpr uint8_t HEADER_EVERY = 25;     // rows between header reprints

const char LEVELS[] = " .:-=+*#%@";
constexpr uint8_t NUM_LEVELS = sizeof(LEVELS) - 1;

RF24 radio(PIN_CE, PIN_CSN);
uint8_t hits[NUM_CHANNELS];
uint16_t rows = 0;

void printHeader() {
  // Frequency ruler: a digit every 10 MHz (0 = 2400, 1 = 2410, ...).
  Serial.print("\n MHz 2400      2410      2420      2430      2440      2450      2460      2470      2480      2490      2500      2510      2520\n");
  Serial.print("     ");
  for (uint8_t ch = 0; ch < NUM_CHANNELS; ch++) Serial.print(ch % 10 == 0 ? '|' : ' ');
  Serial.println();
  // WiFi 1 / 6 / 11 centers (2412 / 2437 / 2462 MHz).
  Serial.print("WiFi ");
  for (uint8_t ch = 0; ch < NUM_CHANNELS; ch++) {
    char c = ' ';
    if (ch == 12) c = '1';
    if (ch == 37) c = '6';
    if (ch == 62) c = 'B';  // channel 11
    Serial.print(c);
  }
  Serial.println("   (B = WiFi ch 11)");
}

void setup() {
  Serial.begin(115200);
  delay(500);
  pinMode(PIN_CSN_UNUSED, OUTPUT);
  digitalWrite(PIN_CSN_UNUSED, HIGH);

  SPI.begin();  // SCK=18, MISO=19, MOSI=23
  if (!radio.begin(&SPI) || !radio.isChipConnected()) {
    Serial.println("Radio not found on CE=4/CSN=5. Check wiring.");
    while (true) delay(1000);
  }
  radio.setAutoAck(false);
  radio.setDataRate(RF24_1MBPS);
  radio.startListening();
  radio.stopListening();
  Serial.println("nRF24L01+ 2.4 GHz spectrum analyzer");
}

void loop() {
  memset(hits, 0, sizeof(hits));
  for (uint8_t pass = 0; pass < PASSES; pass++) {
    for (uint8_t ch = 0; ch < NUM_CHANNELS; ch++) {
      radio.setChannel(ch);
      radio.startListening();
      delayMicroseconds(130);  // RX settling + RPD sampling time
      if (radio.testRPD()) hits[ch]++;
      radio.stopListening();
    }
  }

  if (rows % HEADER_EVERY == 0) printHeader();
  rows++;

  uint8_t peakCh = 0;
  Serial.print("    |");
  for (uint8_t ch = 0; ch < NUM_CHANNELS; ch++) {
    uint8_t level = (uint16_t)hits[ch] * (NUM_LEVELS - 1) / PASSES;
    if (hits[ch] > 0 && level == 0) level = 1;  // show any detection
    Serial.print(LEVELS[level]);
    if (hits[ch] > hits[peakCh]) peakCh = ch;
  }
  Serial.print('|');
  if (hits[peakCh] > 0) {
    Serial.printf(" peak %u MHz (%u%%)", 2400 + peakCh, hits[peakCh] * 100 / PASSES);
  }
  Serial.println();
}
